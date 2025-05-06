import cv2
import numpy as np
import time
import threading
import queue
import requests
import argparse
import os
import logging
import base64
import json
import uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from contextlib import asynccontextmanager
from onvif import ONVIFCamera
import asyncio
from time import perf_counter  

# Biến toàn cục để lưu thông tin độ trễ
latency_stats = {
    "frame_acquisition": [],    # Độ trễ đọc frame từ camera
    "processing": [],           # Độ trễ xử lý frame
    "encoding": [],             # Độ trễ mã hóa JPEG
    "websocket_send": [],       # Độ trễ gửi qua WebSocket
    "total": [],                # Tổng độ trễ
    "rtsp_to_processing": [],   # Độ trễ từ RTSP đến lúc xử lý
    "samples": 0                # Số mẫu đã thu thập
}

# Hàm tính trung bình của danh sách, bỏ qua 10% giá trị cao nhất
def calculate_average(values, trim_percent=10):
    if not values:
        return 0
    sorted_values = sorted(values)
    trim_count = int(len(sorted_values) * trim_percent / 100)
    trimmed_values = sorted_values[:-trim_count] if trim_count > 0 else sorted_values
    return sum(trimmed_values) / len(trimmed_values)

# Thiết lập logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler("traffic_system.log")])
logger = logging.getLogger("SmartTraffic")

# ThingSpeak configuration
THINGSPEAK_WRITE_API_KEY = "0Y4ASEUMTBHFQ8I5"
THINGSPEAK_URL = "https://api.thingspeak.com/update"

# Giá trị mặc định
DEFAULT_IP = "192.168.43.81"
#192.168.1.14
DEFAULT_PORT = 554
DEFAULT_ONVIF_PORT = 80
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "L2A17879"
DEFAULT_CHANNEL = 1
DEFAULT_STREAM = 1  # Sub stream
DEFAULT_YOLO_MODEL = "yolov8n.pt"

# Các biến toàn cục
current_direction = 0  # 0: hướng mặc định, 1: hướng vuông góc
frame_skip = 1  # Xử lý 1 frame, bỏ qua frame_skip frames
process_queue = queue.Queue(maxsize=10)  # Hàng đợi xử lý frame
results_queue = queue.Queue(maxsize=5)  # Hàng đợi kết quả - giới hạn kích thước

# Thêm các biến định nghĩa hướng
direction_setup = {
    "initialized": False,  # Đã định nghĩa hướng chưa
    "user_defined": False, # Hướng có được định nghĩa bởi người dùng không
    "direction1_angle": 0, # Góc quay của hướng 1 (để lưu cấu hình)
    "direction2_angle": 0  # Góc quay của hướng 2
}

# Thêm vào phần khai báo biến toàn cục
session_statistics = {
    "direction1": {
        "total_density": 0.0,
        "density_count": 0,  # Đếm số lần cập nhật
        "session_start": time.time()
    },
    "direction2": {
        "total_density": 0.0,
        "density_count": 0,
        "session_start": time.time()
    }
}

# Thông tin đèn giao thông
traffic_lights = {
    "direction1": {"state": "green", "time_left": 60},  # Hướng mặc định
    "direction2": {"state": "red", "time_left": 60}     # Hướng vuông góc
}

# Thay đổi biến camera_cycle
camera_cycle = {
    "fixed_position_time": 30,  # Thời gian đứng yên tại mỗi vị trí (10 giây)
    "last_position_change": 0,  # Thời điểm chuyển vị trí gần nhất
    "current_cycle_step": 0,    # Bước hiện tại trong chu trình (0-3)
    "active": True,             # Camera đang hoạt động hay trong chế độ delay
    "measurement_active": True, # Có đang đo lưu lượng không
    "current_cycle_count": 0,   # Đếm số chu kỳ trong một đợt đo (0-3)
    "total_cycles": 4,          # Tổng số chu kỳ cần đo trong một đợt (2 hướng, mỗi hướng 2 lần)
    "direction_counts": {       # Lưu tổng xe đếm được cho mỗi hướng trong đợt hiện tại
        "direction1": 0,
        "direction2": 0
    }
}
vehicle_counting_data = {
    "cycle_counts": {
        "direction1": [0, 0],       # [Đếm lần 1, Đếm lần 2]
        "direction2": [0, 0]        # [Đếm lần 1, Đếm lần 2]
    },
    "cycle_ids": {
        "direction1": [set(), set()], # [IDs lần 1, IDs lần 2]
        "direction2": [set(), set()]  # [IDs lần 1, IDs lần 2]
    },
    "current_cycle_index": 0        # 0-3: Chỉ số chu kỳ hiện tại
}
def get_current_count_indices():
    """Trả về (direction_index, cycle_index) cho lần đếm hiện tại"""
    direction_index = 0 if camera_cycle["current_cycle_step"] % 2 == 0 else 1  # 0: direction1, 1: direction2
    cycle_index = 0 if camera_cycle["current_cycle_step"] < 2 else 1  # 0: lần đếm 1, 1: lần đếm 2
    return direction_index, cycle_index

# Cập nhật cycle_control
cycle_control = {
    "cycles_completed": 0,        # Số chu kỳ quay camera đã hoàn thành
    "required_cycles": 4,         # Số chu kỳ cần hoàn thành trước khi tính toán (2 hướng, mỗi hướng 2 lần)
    "delay_after_reset": 60,      # Thời gian delay sau khi reset (60 giây = 1 phút)
    "total_cycle_time": 120,      # Tổng thời gian của chu kỳ đèn (giây)
    "default_green_time": 60,     # Thời gian đèn xanh mặc định (giây)
    "density_factor": 100,        # Hệ số nhân với chênh lệch mật độ
    "last_reset_time": time.time(), # Thời điểm reset gần nhất
    "in_delay_mode": False,       # Đang trong chế độ delay hay không
    "delay_end_time": 0,          # Thời điểm kết thúc delay
    "delay_remaining": 0,         # Số giây còn lại của thời gian delay
    "density_diff": 0.0,          # Chênh lệch mật độ giữa hai làn
    "next_session_config": {      # Cấu hình cho phiên tiếp theo
        "direction1_green_time": 60,
        "direction2_green_time": 60,
        "ready": False            # Đã tính toán xong cấu hình mới chưa
    },
    "current_phase": "measuring"  # Pha hiện tại: "measuring", "calculating", "delaying"
}

# Lưu trữ dữ liệu thống kê
traffic_stats = {
    "direction1": {
        "vehicles": 0,  # Số xe hiện tại
        "total_vehicles": 0,  # Tổng số xe đã đếm
        "density": 0.0,
        "history": [],  # Lưu lịch sử số lượng xe
        "avg_density": 0.0,  # Mật độ trung bình
        "vehicle_types": {},  # Thống kê loại phương tiện
        "session_density": 0.0
    },
    "direction2": {
        "vehicles": 0,
        "total_vehicles": 0,
        "density": 0.0,
        "history": [],
        "avg_density": 0.0,
        "vehicle_types": {},
        "session_density": 0.0
    }
}

# Cài đặt thông số điều khiển giao thông
traffic_control_settings = {
    "min_green_time": 20,  # Thời gian đèn xanh tối thiểu (giây)
    "max_green_time": 100,  # Thời gian đèn xanh tối đa (giây)
    "yellow_time": 3,      # Thời gian đèn vàng
    "history_length": 10,  # Số phần tử lịch sử cần lưu
    "density_weight": 0.7, # Trọng số cho mật độ khi tính thời gian đèn
    "vehicles_weight": 0.3, # Trọng số cho số lượng xe khi tính thời gian đèn
    "count_line_position": 0.6,     # Vị trí đường đếm (tỷ lệ chiều cao frame)
    "vehicle_persist_time": 2.0,    # Thời gian tối đa phương tiện được lưu (giây)
    "min_distance_for_new_id": 50,  # Khoảng cách tối thiểu để xác định phương tiện mới
    "id_purge_interval": 30,         # Thời gian làm sạch ID cũ (giây)
    "direction1_green_time": 60,
    "direction2_green_time": 60
}

# Biến đếm tích lũy phương tiện
cumulative_counts = {
    "direction1": {
        "total_vehicles": 0,
        "last_frame_vehicles": [],  # Danh sách ID các phương tiện trong frame trước
        "counted_ids": set(),       # Set lưu ID các phương tiện đã đếm
        "last_reset_time": time.time()
    },
    "direction2": {
        "total_vehicles": 0,
        "last_frame_vehicles": [],
        "counted_ids": set(),
        "last_reset_time": time.time()
    }
}

# Biến điều khiển
running = True
last_switch_time = time.time()
last_camera_move_time = time.time()
camera_move_lock = threading.Lock()
camera_at_position = threading.Event()
camera_at_position.set()  # Ban đầu camera được coi là ở đúng vị trí

# Trạng thái camera
camera_status = {
    "last_direction": 0,
    "last_move_time": 0,
    "move_success": True,
    "verify_attempts": 0
}



# Biến toàn cục cho camera và detector
cap = None
ptz_controller = None
detector = None
active_connections = set()

class WebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Active connections: {len(self.active_connections)}")
        
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected. Active connections: {len(self.active_connections)}")
        
        # 1. Sửa hàm connect_camera() để giảm độ trễ RTSP nhưng không ảnh hưởng đến chất lượng
    def connect_camera():
        """Kết nối đến camera"""
        global cap
        
        try:
            # Nếu đã kết nối, giải phóng trước
            if cap is not None:
                cap.release()
                
            # Kết nối mới với thiết lập giảm độ trễ
            rtsp_url = f"rtsp://{DEFAULT_USERNAME}:{DEFAULT_PASSWORD}@{DEFAULT_IP}:{DEFAULT_PORT}/cam/realmonitor?channel={DEFAULT_CHANNEL}&subtype={DEFAULT_STREAM}"
            logger.info(f"Đang kết nối đến camera: {rtsp_url}")
            
            # Thiết lập FFMPEG để giảm độ trễ nhưng vẫn giữ chất lượng
            # Giảm buffer_size để giảm độ trễ, nhưng không quá nhỏ
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp|buffer_size;131072|max_delay;500000"
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            
            # Thiết lập buffer size nhỏ để giảm độ trễ
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            
            if not cap.isOpened():
                logger.error("Không thể kết nối đến camera!")
                # Sử dụng camera mặc định nếu không kết nối được
                cap = cv2.VideoCapture(0)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                
                if not cap.isOpened():
                    logger.error("Không thể sử dụng camera mặc định!")
                    return False
                    
            logger.info("Đã kết nối đến camera thành công")
            return True
        except Exception as e:
            logger.error(f"Lỗi khi kết nối camera: {e}")
            return False

    # 2. Sửa hàm process_frame_async() để cân bằng chất lượng và độ trễ
    async def process_frame_async():
        """Xử lý frame từ camera và gửi đến client qua WebSocket"""
        global running, current_direction, latency_stats
        
        frame_count = 0
        frame_skip = 0  # Không bỏ qua frame
        last_stats_update = time.time()
        stats_interval = 1  # Giảm khoảng thời gian giữa các lần cập nhật thống kê xuống 1 giây
        
        while running:
            try:
                if cap is None or not cap.isOpened():
                    logger.warning("Camera đã ngắt kết nối, đang thử kết nối lại...")
                    connect_camera()
                    await asyncio.sleep(0.1)
                    continue
                
                # Bắt đầu đo thời gian tổng
                t_start_total = perf_counter()
                
                # Đo thời gian đọc frame
                t_start_read = perf_counter()
                ret, frame = cap.read()
                t_frame_read = perf_counter() - t_start_read
                latency_stats["frame_acquisition"].append(t_frame_read)
                
                if not ret:
                    logger.warning("Không đọc được frame từ camera")
                    await asyncio.sleep(0.01)
                    continue
                    
                # Ghi lại timestamp của frame gốc
                frame_timestamp = time.time()
                
                # Đếm frame để bỏ qua
                frame_count += 1
                if frame_count % (frame_skip + 1) != 0:
                    await asyncio.sleep(0.001)
                    continue
                    
                # Xử lý frame nếu có kết nối WebSocket
                if ws_manager.active_connections:
                    # Đo thời gian xử lý
                    t_start_process = perf_counter()
                    
                    # Giữ nguyên độ phân giải gốc để đảm bảo chất lượng
                    # Chỉ resize nếu frame quá lớn (trên 1280x720)
                    h, w = frame.shape[:2]
                    if w > 1280 or h > 720:
                        ratio = min(1280 / w, 720 / h)
                        small_frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
                    else:
                        small_frame = frame
                    
                    processed_frame, vehicles, vehicle_detections, density, vehicle_types = detector.detect(small_frame)

                    t_process = perf_counter() - t_start_process
                    latency_stats["processing"].append(t_process)

                    # Thêm kiểm tra này để tránh tính số xe trong thời gian delay
                    if camera_cycle["measurement_active"]:
                        # Cập nhật thống kê
                        update_traffic_stats(current_direction, vehicles, vehicle_detections, density, vehicle_types)
                    else:
                        # Vẫn hiển thị kết quả phát hiện nhưng không cập nhật thống kê
                        logger.debug("Không cập nhật thống kê do đang trong chế độ delay")
                    
                    # Đo thời gian mã hóa - Sử dụng chất lượng JPEG cao hơn (70)
                    t_start_encode = perf_counter()
                    _, encoded_frame = cv2.imencode('.jpg', processed_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    encoded_frame_str = base64.b64encode(encoded_frame).decode('utf-8')
                    t_encode = perf_counter() - t_start_encode
                    latency_stats["encoding"].append(t_encode)
                    
                    # Đo thời gian gửi qua WebSocket
                    t_start_send = perf_counter()
                    await ws_manager.broadcast({
                        "type": "frame", 
                        "data": encoded_frame_str, 
                        "timestamp": frame_timestamp  # Sử dụng timestamp của frame gốc
                    })
                    t_send = perf_counter() - t_start_send
                    latency_stats["websocket_send"].append(t_send)
                    
                    # Tổng thời gian xử lý trong backend
                    t_total = perf_counter() - t_start_total
                    latency_stats["total"].append(t_total)
                    
                    # Tính độ trễ từ RTSP đến xử lý (RTSP internal buffering)
                    t_rtsp_to_processing = frame_timestamp - (time.time() - t_total)
                    latency_stats["rtsp_to_processing"].append(t_rtsp_to_processing)
                    
                    # Tăng số mẫu
                    latency_stats["samples"] += 1
                    
                    # Cập nhật và gửi thống kê thường xuyên hơn
                    current_time = time.time()
                    if current_time - last_stats_update >= stats_interval:
                        update_traffic_lights()
                        await ws_manager.send_stats()
                        
                        last_stats_update = current_time
                
                # Đợi một khoảng thời gian ngắn - đủ để xử lý nhưng không tạo độ trễ
                await asyncio.sleep(0.005)
                
            except Exception as e:
                logger.error(f"Lỗi khi xử lý frame: {e}")
                await asyncio.sleep(0.1)

    # 3. Sửa phương thức send_frame trong WebSocketManager để duy trì chất lượng hình ảnh
    async def send_frame(self, frame):
        """Send encoded frame to all connected clients với chất lượng cao hơn"""
        if not self.active_connections:
            return
            
        try:
            # Encode the frame to JPEG với chất lượng cao
            _, encoded_frame = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            # Convert to base64 string
            encoded_frame_str = base64.b64encode(encoded_frame).decode('utf-8')
            
            # Send to all clients với timestamp
            await self.broadcast({
                "type": "frame", 
                "data": encoded_frame_str,
                "timestamp": time.time()
            })
        except Exception as e:
            logger.error(f"Error sending frame: {e}")

    # 4. Sửa cơ chế broadcast để tối ưu hiệu suất
    async def broadcast(self, data: dict):
        """Gửi dữ liệu đến tất cả các kết nối, tối ưu để giảm độ trễ nhưng không làm giảm chất lượng"""
        disconnected_clients = []
        
        # Nếu không có người dùng kết nối, không cần broadcast
        if not self.active_connections:
            return
        
        # Chỉ chuyển đổi JSON một lần cho tất cả các kết nối
        try:
            json_data = json.dumps(data)
            
            # Tối ưu: Gửi đồng thời đến tất cả các kết nối
            tasks = []
            for connection in self.active_connections:
                tasks.append(self.send_to_client(connection, json_data, disconnected_clients))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                    
        except Exception as e:
            logger.error(f"Error serializing JSON: {e}")
        
        # Xóa các kết nối bị ngắt
        for client in disconnected_clients:
            self.disconnect(client)
                
    async def send_to_client(self, connection, json_data, disconnected_clients):
        """Gửi dữ liệu đến một client cụ thể"""
        try:
            await connection.send_text(json_data)
        except WebSocketDisconnect:
            disconnected_clients.append(connection)
        except Exception as e:
            logger.error(f"Error sending to WebSocket: {e}")
            disconnected_clients.append(connection)
    async def send_frame(self, frame):
        """Send encoded frame to all connected clients với chất lượng cao hơn"""
        if not self.active_connections:
            return
            
        try:
            # Encode the frame to JPEG với chất lượng cao
            _, encoded_frame = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            # Convert to base64 string
            encoded_frame_str = base64.b64encode(encoded_frame).decode('utf-8')
            
            # Send to all clients với timestamp
            await self.broadcast({
                "type": "frame", 
                "data": encoded_frame_str,
                "timestamp": time.time()
            })
        except Exception as e:
            logger.error(f"Error sending frame: {e}")

    async def send_stats(self):
        """Send current stats to all connected clients"""
        if not self.active_connections:
            return
        
        # Cập nhật thời gian còn lại của pha delay
        if cycle_control["in_delay_mode"]:
            update_delay_remaining()
            
        vehicle_counting_data_json = {
            "cycle_counts": vehicle_counting_data["cycle_counts"],
            "current_cycle_index": vehicle_counting_data["current_cycle_index"],
            "cycle_ids": {
                "direction1": [list(s) for s in vehicle_counting_data["cycle_ids"]["direction1"]],
                "direction2": [list(s) for s in vehicle_counting_data["cycle_ids"]["direction2"]]
            }
        }
        
        stats_data = {
            "type": "stats",
            "data": {
                "traffic_stats": traffic_stats,
                "traffic_lights": traffic_lights,
                "camera_cycle": {
                    "current_cycle_step": camera_cycle["current_cycle_step"],
                    "total_cycles": camera_cycle["total_cycles"],
                    "active": camera_cycle["active"],
                    "direction_counts": camera_cycle["direction_counts"]
                },
                "cycle_control": {
                    "current_phase": cycle_control["current_phase"],
                    "in_delay_mode": cycle_control["in_delay_mode"],
                    "delay_remaining": cycle_control["delay_remaining"],
                    "next_config": cycle_control["next_session_config"]
                },
                "current_direction": current_direction,
                # Sử dụng phiên bản đã chuyển đổi
                "vehicle_counting_data": vehicle_counting_data_json
            }
        }

        await self.broadcast(stats_data)


class ImouCameraPTZ:
    def __init__(self, ip, port, username, password):
        self.ip = ip
        self.port = port
        self.username = username
        self.password = password
        self.camera = None
        self.ptz_service = None
        self.media_service = None
        self.profile_token = None
        self.connected = False
        self.last_direction = None
        self.exact_positions = {
            0: None,  # Vị trí chính xác của hướng 1
            1: None   # Vị trí chính xác của hướng 2
        }
        self.position_tolerance = 0.05  # Dung sai góc quay (radian)
        self.max_adjustment_attempts = 3  # Số lần điều chỉnh tối đa
        
    def connect_onvif(self):
        """Kết nối camera qua ONVIF để điều khiển PTZ"""
        if self.connected:
            return True
            
        try:
            self.camera = ONVIFCamera(self.ip, self.port, self.username, self.password)
            
            # Lấy các service cần thiết
            self.media_service = self.camera.create_media_service()
            self.ptz_service = self.camera.create_ptz_service()
            
            # Lấy profile token - sử dụng profile đầu tiên
            profiles = self.media_service.GetProfiles()
            if not profiles:
                logger.error("Không tìm thấy profile camera")
                return False
                
            self.profile_token = profiles[0].token
            self.connected = True
            
            logger.info(f"Đã kết nối ONVIF thành công với camera {self.ip}")
            logger.info(f"Profile token: {self.profile_token}")
            return True
            
        except Exception as e:
            logger.error(f"Lỗi kết nối ONVIF: {e}")
            self.connected = False
            return False
    
    def move_continuous(self, pan_speed, tilt_speed, duration=0.5):
        """Di chuyển camera liên tục với tốc độ xác định"""
        if not self.connected and not self.connect_onvif():
            logger.error("Không thể kết nối đến camera để di chuyển")
            return False
            
        try:
            request = self.ptz_service.create_type('ContinuousMove')
            request.ProfileToken = self.profile_token
            request.Velocity = {
                'PanTilt': {'x': pan_speed, 'y': tilt_speed},
            }
            
            # Gửi lệnh di chuyển
            self.ptz_service.ContinuousMove(request)
            
            # Dừng lại sau khoảng thời gian
            time.sleep(duration)
            self.stop_move()
            
            # Đợi camera ổn định
            time.sleep(0.5)
            
            return True
            
        except Exception as e:
            logger.error(f"Lỗi di chuyển camera: {e}")
            self.connected = False  # Đánh dấu mất kết nối để kết nối lại lần sau
            return False
    
    
    
    def stop_move(self):
        """Dừng chuyển động của camera"""
        if not self.connected:
            return False
            
        try:
            request = self.ptz_service.create_type('Stop')
            request.ProfileToken = self.profile_token
            request.PanTilt = True
            self.ptz_service.Stop(request)
            return True
            
        except Exception as e:
            logger.error(f"Lỗi dừng camera: {e}")
            self.connected = False
            return False
    
    def get_status(self):
        """Lấy trạng thái hiện tại của camera"""
        if not self.connected and not self.connect_onvif():
            return None
            
        try:
            status = self.ptz_service.GetStatus({'ProfileToken': self.profile_token})
            return status
        except Exception as e:
            logger.error(f"Lỗi lấy trạng thái: {e}")
            self.connected = False
            return None
    
    def get_position(self):
        """Lấy vị trí hiện tại của camera (góc pan/tilt)"""
        if not self.connected and not self.connect_onvif():
            return None
            
        try:
            status = self.ptz_service.GetStatus({'ProfileToken': self.profile_token})
            if status and hasattr(status, 'Position') and hasattr(status.Position, 'PanTilt'):
                return {'pan': status.Position.PanTilt.x, 'tilt': status.Position.PanTilt.y}
            return None
        except Exception as e:
            logger.error(f"Lỗi lấy vị trí: {e}")
            self.connected = False
            return None
    
    def move_to_direction(self, direction):
        """Di chuyển camera đến hướng xác định sử dụng ContinuousMove thay vì preset"""
        if direction == self.last_direction:
            logger.info(f"Camera đã ở hướng {direction}, không cần di chuyển")
            return True
            
        camera_at_position.clear()  # Đánh dấu camera đang di chuyển
        
        if direction == 0:  # Hướng mặc định (trực diện)
            success = self.move_continuous(-0.5, 0, 2.0)  # Quay trái 2 giây
        elif direction == 1:  # Hướng vuông góc
            success = self.move_continuous(0.5, 0, 2.0)   # Quay phải 2 giây
        else:
            logger.error(f"Hướng không hợp lệ: {direction}")
            camera_at_position.set()  # Đánh dấu camera đã hoàn tất (thất bại)
            return False
        
        # Cập nhật hướng cuối cùng nếu thành công
        if success:
            self.last_direction = direction
            
        # Đợi một khoảng thời gian để camera ổn định
        time.sleep(1.0)
        camera_at_position.set()  # Đánh dấu camera đã hoàn tất di chuyển
        
        return success
    
    def define_direction(self, direction_number):
        """Định nghĩa hướng camera hiện tại là hướng được chỉ định"""
        global direction_setup
        
        try:
            # Thay vì dựa vào vị trí thực tế từ camera, ta gán các giá trị giả định khác nhau
            if direction_number == 1:
                # Định nghĩa hướng 1 với giá trị cố định
                direction_setup["direction1_angle"] = -0.5  # Giá trị âm cho hướng trái
                self.exact_positions[0] = {'pan': -0.5, 'tilt': 0.8}
                logger.info(f"Đã định nghĩa hướng 1 với góc pan: -0.5, tilt: 0.8")
            elif direction_number == 2:
                # Định nghĩa hướng 2 với giá trị cố định khác
                direction_setup["direction2_angle"] = 0.5   # Giá trị dương cho hướng phải
                self.exact_positions[1] = {'pan': 0.5, 'tilt': 0.8}
                logger.info(f"Đã định nghĩa hướng 2 với góc pan: 0.5, tilt: 0.8")
            else:
                logger.error(f"Số hướng không hợp lệ: {direction_number}")
                return False
            
            # Đánh dấu đã định nghĩa hướng
            direction_setup["initialized"] = True
            direction_setup["user_defined"] = True
            
            return True
        except Exception as e:
            logger.error(f"Lỗi khi định nghĩa hướng {direction_number}: {e}")
            return False
    
    def verify_position(self, direction):
        """Xác minh camera đang ở đúng vị trí của hướng đã định nghĩa"""
        # Đơn giản hóa: luôn trả về True vì chúng ta di chuyển camera đến các vị trí tương đối
        logger.debug(f"Xác minh vị trí: Camera được xác nhận ở hướng {direction+1}")
        return True
        
    def move_to_user_defined_direction(self, direction):
        """Di chuyển camera đến hướng xác định sử dụng cách tiếp cận đơn giản hơn"""
        if direction == self.last_direction:
            logger.info(f"Camera đã ở vị trí {direction+1}, không cần di chuyển")
            return True
        
        camera_at_position.clear()  # Đánh dấu camera đang di chuyển
        
        try:
            # Đơn giản hóa: sử dụng hướng và tốc độ cố định
            if direction == 0:  # Vị trí 1
                # Di chuyển sang trái
                logger.info(f"Di chuyển camera đến vị trí 1 (trái)")
                success = self.move_continuous(-0.5, 0, 3.0)  # Tăng thời gian di chuyển để đủ quay 110 độ
            else:  # Vị trí 2
                # Di chuyển sang phải
                logger.info(f"Di chuyển camera đến vị trí 2 (phải)")
                success = self.move_continuous(0.5, 0, 3.0)  # Tăng thời gian di chuyển để đủ quay 110 độ
            
            # Cập nhật hướng cuối cùng nếu thành công
            if success:
                self.last_direction = direction
                logger.info(f"Đã di chuyển camera thành công sang vị trí {direction+1}")
            else:
                logger.error(f"Không thể di chuyển camera sang vị trí {direction+1}")
            
            # Đợi camera ổn định
            time.sleep(2.0)
            camera_at_position.set()  # Đánh dấu camera đã hoàn tất di chuyển
            
            return success
        except Exception as e:
            logger.error(f"Lỗi khi di chuyển camera đến vị trí {direction+1}: {e}")
            camera_at_position.set()
            return False

class YOLOv8Detector:
    def __init__(self, model_path="yolov8n.pt", conf_threshold=0.25):
        """
        Khởi tạo YOLOv8 detector
        
        Args:
            model_path: Đường dẫn đến file model
            conf_threshold: Ngưỡng tin cậy
        """
        # Đặt biến môi trường để tránh lỗi OpenMP
        import os
        os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
        
        self.conf_threshold = conf_threshold
        self.model = None
        self.classes = None
        self.vehicle_classes = {
            'car': 2,      # Chỉ số mặc định của coco model
            'motorcycle': 3,
            'bus': 5,
            'truck': 7,
            'bicycle': 1
        }
        
        # Định nghĩa ngược lại từ ID đến tên
        self.id_to_class = {}
        
        # Các biến theo dõi đối tượng
        self.tracker = None
        self.tracked_objects = {}  # Dictionary để lưu các đối tượng đã theo dõi
        self.counted_ids = set()   # Set để lưu ID các đối tượng đã được đếm
        self.total_count = 0       # Tổng số đối tượng đã đếm
        self.vehicle_type_counts = {} # Thống kê theo loại phương tiện
        
        try:
            # Import thư viện ultralytics
            from ultralytics import YOLO
            import torch
            
            # Kiểm tra và cấu hình CUDA nếu có
            if torch.cuda.is_available():
                logger.info("CUDA khả dụng, sử dụng GPU để tăng tốc")
            else:
                logger.info("CUDA không khả dụng, sử dụng CPU")
            
            # Tải model YOLOv8 với cấu hình phù hợp
            self.model = YOLO(model_path)
            logger.info(f"Đã tải YOLOv8 thành công từ {model_path}")
            
            # Lấy thông tin về các class
            self.classes = self.model.names
            
            # Cập nhật id_to_class dựa trên model đã tải
            for class_name, default_id in self.vehicle_classes.items():
                found = False
                for idx, name in self.classes.items():
                    if name.lower() == class_name:
                        self.id_to_class[idx] = class_name
                        found = True
                        break
                # Nếu không tìm thấy, sử dụng ID mặc định
                if not found:
                    self.id_to_class[default_id] = class_name
            
            # Sử dụng model làm tracker - tận dụng ByteTrack tích hợp sẵn trong ultralytics
            self.tracker = self.model
            logger.info(f"Đã cấu hình tracker với {len(self.id_to_class)} loại phương tiện")
            
        except Exception as e:
            logger.error(f"Lỗi khi khởi tạo YOLOv8: {e}")
            logger.warning("Sử dụng phương pháp Background Subtraction thay thế.")
            self.model = None
            
        # Khởi tạo background subtractor dự phòng
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=16, detectShadows=False)

    def detect(self, frame):
        """
        Phát hiện và theo dõi đối tượng trong frame, chỉ đếm khi đối tượng vượt qua đường đếm
        """     
        try:
            # Sử dụng frame gốc mà không resize để giữ chất lượng
            # Chỉ resize nếu frame quá lớn (> 1280x720)
            h, w = frame.shape[:2]
            processing_frame = frame
            if w > 1280 or h > 720:
                ratio = min(1280 / w, 720 / h)
                processing_frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
            
            # Lấy chỉ số của các class phương tiện
            vehicle_indices = list(self.id_to_class.keys())
            
            # Vị trí đường đếm
            height, width = frame.shape[:2]
            count_line_y = int(height * 0.6)  # Mặc định ở 60% chiều cao frame
            if 'traffic_control_settings' in globals() and "count_line_position" in traffic_control_settings:
                count_line_y = int(height * traffic_control_settings["count_line_position"])
            
            # Thực hiện tracking với YOLOv8
            try:
                # Sử dụng tham số tracking với persist=True để theo dõi các đối tượng
                results = self.tracker.track(processing_frame, conf=self.conf_threshold, persist=True, verbose=False)
            except Exception as track_error:
                logger.error(f"Lỗi khi thực hiện tracking: {track_error}")
                # Sử dụng predict nếu track không hoạt động
                results = self.model(processing_frame, conf=self.conf_threshold, verbose=False)
            
            # Danh sách chứa thông tin các phương tiện được phát hiện trong frame hiện tại
            vehicle_detections = []
            vehicle_types = {}  # Thống kê theo loại
            processed_frame = frame.copy()
            
            # Vẽ đường đếm
            cv2.line(processed_frame, (0, count_line_y), (width, count_line_y), (255, 0, 0), 2)
            
            if len(results) > 0:
                boxes = results[0].boxes
                
                for box in boxes:
                    # Lấy class ID và confidence
                    cls_id = int(box.cls.item())
                    conf = float(box.conf.item())
                    
                    # Nếu không phải phương tiện giao thông, bỏ qua
                    if cls_id not in vehicle_indices:
                        continue
                    
                    # Lấy tọa độ bounding box
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    
                    # Lọc các box quá nhỏ (có thể là nhiễu)
                    box_width = x2 - x1
                    box_height = y2 - y1
                    min_size = min(frame.shape[0], frame.shape[1]) * 0.02  # 2% kích thước frame
                    
                    if box_width < min_size or box_height < min_size:
                        continue
                    
                    # Tính toán điểm dưới trung tâm của phương tiện
                    center_x = (x1 + x2) // 2
                    bottom_y = y2
                    
                    # Biến để theo dõi ID
                    track_id = None
                    
                    # Kiểm tra nếu có thuộc tính ID (từ tracking)
                    if hasattr(box, 'id') and box.id is not None:
                        try:
                            track_id = int(box.id.item())
                        except:
                            # Nếu không lấy được ID, tạo ID tạm thời
                            track_id = f"temp_{cls_id}_{center_x}_{bottom_y}"
                    else:
                        # Nếu không có ID tracking, tạo ID tạm thời dựa trên vị trí
                        track_id = f"temp_{cls_id}_{center_x}_{bottom_y}"
                    
                    # Kiểm tra xem đối tượng đã vượt qua đường đếm chưa
                    current_crossed_line = bottom_y > count_line_y
                    
                    # Lưu thông tin của đối tượng theo dõi
                    if track_id not in self.tracked_objects:
                        # Đối tượng mới
                        self.tracked_objects[track_id] = {
                            'crossed_line': current_crossed_line,
                            'class_id': cls_id,
                            'first_seen': True,
                            'last_seen': 0  # Frame counter
                        }
                    else:
                        # Cập nhật thông tin
                        prev_crossed = self.tracked_objects[track_id]['crossed_line']
                        self.tracked_objects[track_id]['first_seen'] = False
                        self.tracked_objects[track_id]['last_seen'] = 0  # Reset counter
                        
                        # Nếu đối tượng vừa vượt qua đường đếm và chưa được đếm trước đó
                        if current_crossed_line and not prev_crossed and track_id not in self.counted_ids:
                            self.counted_ids.add(track_id)
                            self.total_count += 1
                            
                            # Cập nhật thống kê theo loại phương tiện
                            class_name = self.classes[cls_id]
                            if class_name not in self.vehicle_type_counts:
                                self.vehicle_type_counts[class_name] = 0
                            self.vehicle_type_counts[class_name] += 1
                    
                    # Cập nhật trạng thái đã vượt qua đường
                    self.tracked_objects[track_id]['crossed_line'] = current_crossed_line
                    
                    # Thêm vào danh sách phát hiện trong frame hiện tại
                    vehicle_detections.append({
                        'id': track_id,
                        'box': (x1, y1, x2-x1, y2-y1),
                        'center': (center_x, (y1+y2)//2),
                        'bottom_center': (center_x, bottom_y),
                        'class_id': cls_id,
                        'confidence': conf,
                        'crossed_line': current_crossed_line
                    })
                    
                    # Cập nhật thống kê loại phương tiện trong frame hiện tại
                    class_name = self.classes[cls_id]
                    vehicle_types[class_name] = vehicle_types.get(class_name, 0) + 1
                    
                    # Chọn màu dựa trên loại phương tiện
                    colors = {
                        'car': (0, 255, 0),       # Xanh lá
                        'truck': (0, 165, 255),   # Cam
                        'bus': (0, 0, 255),       # Đỏ
                        'motorcycle': (255, 0, 0), # Xanh dương
                        'bicycle': (255, 255, 0)   # Cyan
                    }
                    
                    # Màu đỏ đậm cho đối tượng đã được đếm
                    color = colors.get(class_name.lower(), (0, 255, 0))
                    if track_id in self.counted_ids:
                        color = (0, 0, 255)  # Đỏ đậm cho đối tượng đã đếm
                    
                    # Hiển thị ID và class
                    label = f"ID:{track_id} {class_name}: {conf:.2f}"
                    
                    # Vẽ bounding box
                    cv2.rectangle(processed_frame, (x1, y1), (x2, y2), color, 2)
                    
                    # Vẽ nhãn
                    cv2.putText(processed_frame, label, (x1, y1 - 10), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            
            # Cập nhật counter cho các đối tượng không còn xuất hiện
            objects_to_remove = []
            for obj_id, obj_info in self.tracked_objects.items():
                if obj_id not in [d['id'] for d in vehicle_detections]:
                    obj_info['last_seen'] += 1
                    # Nếu đối tượng không xuất hiện quá 30 frame (1 giây ở 30fps), xóa khỏi danh sách
                    if obj_info['last_seen'] > 30:
                        objects_to_remove.append(obj_id)
            
            # Xóa các đối tượng cũ
            for obj_id in objects_to_remove:
                del self.tracked_objects[obj_id]
            
            # Số lượng phương tiện trong frame
            frame_vehicle_count = len(vehicle_detections)
            
            # Tính mật độ giao thông
            height, width = frame.shape[:2]
            area = height * width//5
            
            # Tính tổng diện tích của tất cả xe
            vehicle_area = 0
            for detection in vehicle_detections:
                x, y, w, h = detection['box']
                vehicle_area += w * h
            
            # Mật độ được tính là tỉ lệ diện tích bị chiếm bởi xe
            density = min(1.0, vehicle_area / (area * 0.5))
            
            # Hiển thị thông tin
            cv2.putText(processed_frame, f"Tong so xe da dem: {self.total_count}", (20, 40), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            cv2.putText(processed_frame, f"Xe trong frame: {frame_vehicle_count}", (20, 70), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)
            cv2.putText(processed_frame, f"Mat do: {density:.2f}", (20, 100), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            # Hiển thị thống kê theo loại phương tiện đã đếm
            y_offset = 130
            for vehicle_type, count in self.vehicle_type_counts.items():
                cv2.putText(processed_frame, f"{vehicle_type}: {count}", (20, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                y_offset += 30
            
            return processed_frame, self.total_count, vehicle_detections, density, self.vehicle_type_counts
            
        except Exception as e:
            logger.error(f"Lỗi khi phát hiện đối tượng với YOLOv8: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return self.detect_with_background_subtraction(frame)

    # Phương thức khởi tạo lại bộ đếm
    def reset_counter(self):
        """
        Khởi tạo lại bộ đếm
        """
        self.tracked_objects = {}
        self.counted_ids = set()
        self.total_count = 0
        self.vehicle_type_counts = {}

   
def update_total_vehicle_count(direction, count):
    """Cập nhật tổng số xe đếm được cho hướng"""
    if direction == 0:  # direction1
        camera_cycle["direction_counts"]["direction1"] += count
    else:  # direction2
        camera_cycle["direction_counts"]["direction2"] += count
    logger.info(f"Đã cập nhật tổng số xe cho hướng {direction+1}: +{count}, tổng: {camera_cycle['direction_counts'][f'direction{direction+1}']}")

def calculate_traffic_light_times():
    """Tính toán thời gian đèn giao thông cân bằng và ổn định hơn"""
    global cycle_control, traffic_control_settings, vehicle_counting_data
    
    # --- 1. Lấy số liệu từ cấu trúc đếm mới ---
    try:
        direction1_count1 = vehicle_counting_data["cycle_counts"]["direction1"][0] 
        direction1_count2 = vehicle_counting_data["cycle_counts"]["direction1"][1]
        direction2_count1 = vehicle_counting_data["cycle_counts"]["direction2"][0]
        direction2_count2 = vehicle_counting_data["cycle_counts"]["direction2"][1]
    except (KeyError, IndexError):
        # Đảm bảo tương thích ngược trong trường hợp cấu trúc mới chưa được khởi tạo đúng
        logger.warning("Sử dụng camera_cycle.direction_counts vì không có dữ liệu cycle_counts")
        direction1_count1 = direction1_count2 = camera_cycle["direction_counts"]["direction1"] / 2
        direction2_count1 = direction2_count2 = camera_cycle["direction_counts"]["direction2"] / 2
    
    # Tính trung bình
    direction1_avg_count = (direction1_count1 + direction1_count2) / 2
    direction2_avg_count = (direction2_count1 + direction2_count2) / 2
    
    logger.info(f"Đếm xe chi tiết: Hướng 1 ({direction1_count1}, {direction1_count2}), " + 
               f"Hướng 2 ({direction2_count1}, {direction2_count2})")
    logger.info(f"Trung bình xe: Hướng 1 = {direction1_avg_count:.1f}, Hướng 2 = {direction2_avg_count:.1f}")
    
    # --- Các bước tính toán giống như trước ---
    # Xác định phạm vi tối thiểu và tối đa
    min_time = traffic_control_settings["min_green_time"]
    max_time = traffic_control_settings["max_green_time"]
    total_green_time = cycle_control["total_cycle_time"] - (traffic_control_settings["yellow_time"] * 2)
    
    # Tính tỷ lệ
    total_count = direction1_avg_count + direction2_avg_count
    if total_count < 1:
        # Nếu không có xe, sử dụng tỷ lệ 50:50
        direction1_ratio = direction2_ratio = 0.5
    else:
        # Thêm hệ số cân bằng để tránh thay đổi quá mạnh
        base_ratio = 0.3  # Hệ số cơ bản - mỗi hướng ít nhất 30%
        variable_ratio = 1 - 2 * base_ratio  # Phần còn lại phân bổ theo xe
        
        direction1_ratio = base_ratio + variable_ratio * (direction1_avg_count / total_count)
        direction2_ratio = base_ratio + variable_ratio * (direction2_avg_count / total_count)
    
    # Tính toán thời gian ban đầu
    raw_time1 = int(total_green_time * direction1_ratio)
    raw_time2 = int(total_green_time * direction2_ratio)
    
    # Làm mịn với chu kỳ trước
    prev_time1 = traffic_control_settings.get("direction1_green_time", cycle_control["default_green_time"])
    prev_time2 = traffic_control_settings.get("direction2_green_time", cycle_control["default_green_time"])
    
    # Hệ số làm mịn (30% mới, 70% cũ)
    smooth_factor = 0.5
    
    # Áp dụng làm mịn
    smooth_time1 = int(prev_time1 * (1 - smooth_factor) + raw_time1 * smooth_factor)
    smooth_time2 = 114- smooth_time1
    #int(prev_time2 * (1 - smooth_factor) + raw_time2 * smooth_factor)
    
    # Áp dụng giới hạn
    final_time1 = max(min_time, min(max_time, smooth_time1))
    final_time2 = max(min_time, min(max_time, smooth_time2))
    
    # Cập nhật cấu hình
    cycle_control["next_session_config"]["direction1_green_time"] = final_time1
    cycle_control["next_session_config"]["direction2_green_time"] = final_time2
    cycle_control["next_session_config"]["ready"] = True
    
    red_time_direction1 = final_time2 + (traffic_control_settings["yellow_time"] * 2)
    
    # Gửi dữ liệu lên ThingSpeak sau khi hoàn thành 4 lượt xoay
    send_data_to_thingspeak(final_time1, red_time_direction1)
    
    logger.info(f"Thời gian đèn mới: Hướng 1 = {final_time1}s, Hướng 2 = {final_time2}s")
    return final_time1, final_time2

def reset_counting_data():
    """Reset dữ liệu đếm xe cho chu kỳ mới"""
    global vehicle_counting_data
    
    # Reset số lượng và ID đã đếm
    vehicle_counting_data = {
        "cycle_counts": {
            "direction1": [0, 0],
            "direction2": [0, 0]
        },
        "cycle_ids": {
            "direction1": [set(), set()],
            "direction2": [set(), set()]
        },
        "current_cycle_index": 0
    }
    
    logger.info("Đã reset dữ liệu đếm xe cho chu kỳ mới")


def start_delay_phase():
    """Bắt đầu pha delay 1 phút và reset dữ liệu đếm"""
    global cycle_control, camera_cycle, vehicle_counting_data, traffic_stats, detector, cumulative_counts
    
    current_time = time.time()
    
    # Đánh dấu bắt đầu delay
    cycle_control["in_delay_mode"] = True
    cycle_control["delay_end_time"] = current_time + cycle_control["delay_after_reset"]
    cycle_control["delay_remaining"] = cycle_control["delay_after_reset"]
    cycle_control["current_phase"] = "delaying"
    cycle_control["last_reset_time"] = current_time
    
    # Tắt chế độ đo lường và di chuyển camera
    camera_cycle["active"] = False
    camera_cycle["measurement_active"] = False
    
    # QUAN TRỌNG: Reset đúng cách vehicle_counting_data với global
    global vehicle_counting_data
    vehicle_counting_data = {
        "cycle_counts": {
            "direction1": [0, 0], # Reset đếm số xe cho hướng 1
            "direction2": [0, 0]  # Reset đếm số xe cho hướng 2
        },
        "cycle_ids": {
            "direction1": [set(), set()], # Reset IDs đã đếm cho hướng 1
            "direction2": [set(), set()]  # Reset IDs đã đếm cho hướng 2
        },
        "current_cycle_index": 0
    }
    
    # Reset biến đếm tích lũy
    for direction_key in ["direction1", "direction2"]:
        cumulative_counts[direction_key]["total_vehicles"] = 0
        cumulative_counts[direction_key]["counted_ids"] = set()
        cumulative_counts[direction_key]["last_frame_vehicles"] = []
    
    # Reset số xe đã đếm trong traffic_stats
    for direction_key in ["direction1", "direction2"]:
        traffic_stats[direction_key]["vehicles"] = 0
        traffic_stats[direction_key]["total_vehicles"] = 0
    
    # Reset số xe đã đếm trong camera_cycle
    camera_cycle["direction_counts"]["direction1"] = 0
    camera_cycle["direction_counts"]["direction2"] = 0
    
    # Reset bộ đếm trong detector nếu cần
    if detector:
        detector.reset_counter()
    
    logger.info(f"Bắt đầu pha delay {cycle_control['delay_after_reset']} giây - Đã reset tất cả biến đếm xe")
    
    return True

def update_delay_remaining():
    """Cập nhật thời gian còn lại của pha delay"""
    if cycle_control["in_delay_mode"]:
        current_time = time.time()
        cycle_control["delay_remaining"] = max(0, int(cycle_control["delay_end_time"] - current_time))
        return cycle_control["delay_remaining"]
    return 0

def track_and_count_vehicles(direction, current_detections):
    """Theo dõi và đếm xe với cơ chế chống đếm trùng tốt hơn"""
    global cumulative_counts, vehicle_counting_data
    
    direction_key = f"direction{direction+1}"
    count_data = cumulative_counts[direction_key]
    
    # Xác định đang ở lần đếm thứ mấy (0 hoặc 1) của hướng hiện tại
    direction_index = 0 if direction == 0 else 1
    cycle_index = 0 if camera_cycle["current_cycle_step"] < 2 else 1
    
    # Lấy hoặc khởi tạo set các ID đã đếm của lần đếm này
    if direction_key not in vehicle_counting_data["cycle_ids"]:
        vehicle_counting_data["cycle_ids"][direction_key] = [set(), set()]
    
    unique_ids = vehicle_counting_data["cycle_ids"][direction_key][cycle_index]
    
    # Lấy vị trí hiện tại của các xe
    current_vehicles = []
    current_positions = {}
    
    count_line_crossed = 0
    
    for detection in current_detections:
        vehicle_id = detection['id']
        current_vehicles.append(vehicle_id)
        current_positions[vehicle_id] = detection['bottom_center']
        
        # Kiểm tra xe đã vượt qua đường đếm chưa
        if detection['crossed_line'] and vehicle_id not in unique_ids:
            unique_ids.add(vehicle_id)
            
            # Cập nhật số lượng trong bộ đếm chu kỳ
            if direction_key not in vehicle_counting_data["cycle_counts"]:
                vehicle_counting_data["cycle_counts"][direction_key] = [0, 0]
                
            vehicle_counting_data["cycle_counts"][direction_key][cycle_index] += 1
            
            # Cập nhật số lượng trong bộ đếm tích lũy (duy trì tương thích)
            count_data['total_vehicles'] += 1
            count_line_crossed += 1
            
            logger.info(f"Đã đếm xe {vehicle_id} ở hướng {direction+1}, lần {cycle_index+1}. "+
                       f"Tổng: {vehicle_counting_data['cycle_counts'][direction_key][cycle_index]}")
    
    # Làm sạch ID cũ theo định kỳ
    current_time = time.time()
    if current_time - count_data['last_reset_time'] > traffic_control_settings["id_purge_interval"]:
        count_data['last_reset_time'] = current_time
    
    # Cập nhật danh sách phương tiện frame trước
    count_data['last_frame_vehicles'] = current_vehicles
    
    return len(current_vehicles), count_data['total_vehicles'], count_line_crossed

def update_traffic_stats(direction, vehicles, vehicle_detections, density, vehicle_types):
    """Cập nhật thống kê giao thông với dữ liệu mới"""
    global traffic_stats, session_statistics, cycle_control
    
    direction_key = f"direction{direction+1}"
    stats = traffic_stats[direction_key]
    session_stats = session_statistics[direction_key]
    
    # Kiểm tra xem đang trong giai đoạn delay hay không
    if cycle_control["in_delay_mode"]:
        # Trong giai đoạn delay, chỉ cập nhật các thông số hiển thị nhưng không đếm thêm xe
        stats["vehicles"] = vehicles  # Cập nhật số xe nhìn thấy trong frame
        stats["density"] = density  # Cập nhật mật độ hiện tại
        
        # Cập nhật loại phương tiện để hiển thị nhưng không tăng tổng số
        for vehicle_type, count in vehicle_types.items():
            stats["vehicle_types"][vehicle_type] = count
        
        # Không cập nhật total_vehicles và không gọi track_and_count_vehicles
        logger.debug(f"Trong giai đoạn delay: Chỉ hiển thị {vehicles} xe tại hướng {direction+1}, không đếm thêm")
        return
    
    # Nếu không trong giai đoạn delay, tiếp tục đếm như bình thường
    # Cập nhật bằng cơ chế theo dõi và đếm phương tiện
    current_count, total_count, new_count = track_and_count_vehicles(direction, vehicle_detections)
    
    # Cập nhật giá trị hiện tại
    stats["vehicles"] = current_count
    stats["total_vehicles"] = total_count  # Thêm biến tổng số xe
    stats["density"] = density
    
    # Cập nhật density tổng cho phiên
    session_stats["total_density"] += density
    session_stats["density_count"] += 1
    
    # Tính density trung bình cho toàn phiên
    if session_stats["density_count"] > 0:
        avg_session_density = session_stats["total_density"] / session_stats["density_count"]
        stats["session_density"] = avg_session_density  # Thêm biến density tổng
    else:
        stats["session_density"] = 0.0
    
    # Cập nhật loại phương tiện
    for vehicle_type, count in vehicle_types.items():
        stats["vehicle_types"][vehicle_type] = count
    
    # Cập nhật lịch sử (giới hạn độ dài)
    history_length = traffic_control_settings["history_length"]
    if len(stats["history"]) >= history_length:
        stats["history"].pop(0)  # Loại bỏ phần tử cũ nhất
    
    stats["history"].append({
        "vehicles": current_count, 
        "total_vehicles": total_count,
        "density": density, 
        "timestamp": time.time()
    })
    
    # Tính mật độ trung bình từ lịch sử
    if stats["history"]:
        avg_density = sum(item["density"] for item in stats["history"]) / len(stats["history"])
        stats["avg_density"] = avg_density
    else:
        stats["avg_density"] = density

def calculate_green_time(direction):
    """Tính toán thời gian đèn xanh tối ưu cho hướng được chỉ định"""
    # Kiểm tra xem đã có thời gian đèn xanh được điều chỉnh chưa
    if "direction1_green_time" in traffic_control_settings and "direction2_green_time" in traffic_control_settings:
        if direction == 0:  # Hướng 1
            return traffic_control_settings["direction1_green_time"]
        else:  # Hướng 2
            return traffic_control_settings["direction2_green_time"]
    return 60  # Mặc định 60 giây

def update_traffic_lights():
    """Cập nhật trạng thái đèn giao thông dựa trên lưu lượng"""
    global last_switch_time, traffic_lights
    
    # Giảm thời gian còn lại
    for direction in traffic_lights:
        traffic_lights[direction]["time_left"] = max(0, traffic_lights[direction]["time_left"] - 1)
    
    # Kiểm tra nếu đã đến lúc chuyển đèn
    direction1 = traffic_lights["direction1"]
    direction2 = traffic_lights["direction2"]
    
    # Nếu đèn xanh đã hết thời gian, chuyển sang đèn vàng
    if (direction1["state"] == "green" and direction1["time_left"] <= 0) or \
       (direction2["state"] == "green" and direction2["time_left"] <= 0):
        
        if direction1["state"] == "green":
            direction1["state"] = "yellow"
            direction1["time_left"] = traffic_control_settings["yellow_time"]
            logger.info("Đèn hướng 1 chuyển từ XANH sang VÀNG")
        elif direction2["state"] == "green":
            direction2["state"] = "yellow"
            direction2["time_left"] = traffic_control_settings["yellow_time"]
            logger.info("Đèn hướng 2 chuyển từ XANH sang VÀNG")
    
    # Nếu đèn vàng đã hết thời gian, chuyển sang đèn đỏ và đèn đối diện thành xanh
    elif (direction1["state"] == "yellow" and direction1["time_left"] <= 0) or \
         (direction2["state"] == "yellow" and direction2["time_left"] <= 0):
        
        if direction1["state"] == "yellow":
            direction1["state"] = "red"
            direction2["state"] = "green"
            
            # Tính toán thời gian đèn xanh tối ưu
            green_time = calculate_green_time(1)  # Hướng 2
            
            direction2["time_left"] = green_time
            direction1["time_left"] = green_time
            last_switch_time = time.time()
            
            logger.info(f"Chuyển đèn: Hướng 2 XANH trong {green_time}s")
            
        elif direction2["state"] == "yellow":
            direction2["state"] = "red"
            direction1["state"] = "green"
            
            # Tính toán thời gian đèn xanh tối ưu
            green_time = calculate_green_time(0)  # Hướng 1
            
            direction1["time_left"] = green_time
            direction2["time_left"] = green_time
            last_switch_time = time.time()
            
            logger.info(f"Chuyển đèn: Hướng 1 XANH trong {green_time}s")

def reset_traffic_stats():
    """Reset dữ liệu thống kê giao thông"""
    global traffic_stats, cumulative_counts, session_statistics
    
    # Reset số xe và mật độ
    for direction in ["direction1", "direction2"]:
        # Reset thống kê xe
        traffic_stats[direction]["total_vehicles"] = 0
        traffic_stats[direction]["session_density"] = 0.0
        traffic_stats[direction]["history"] = []
        traffic_stats[direction]["vehicle_types"] = {}
        
        # Reset dữ liệu đếm
        cumulative_counts[direction]["total_vehicles"] = 0
        cumulative_counts[direction]["counted_ids"] = set()
        cumulative_counts[direction]["last_frame_vehicles"] = []
        
        # Reset thống kê phiên
        session_statistics[direction]["total_density"] = 0.0
        session_statistics[direction]["density_count"] = 0
    
    logger.info("Đã reset dữ liệu thống kê giao thông")

def check_delay_status():
    """Kiểm tra trạng thái delay và áp dụng cấu hình mới khi kết thúc"""
    global cycle_control, traffic_control_settings, camera_cycle, vehicle_counting_data, detector
    
    current_time = time.time()
    
    # Kiểm tra xem có đang trong chế độ delay không
    if cycle_control["in_delay_mode"]:
        # Kiểm tra xem đã hết thời gian delay chưa
        if current_time >= cycle_control["delay_end_time"]:
            # Kết thúc chế độ delay
            cycle_control["in_delay_mode"] = False
            
            # Kích hoạt lại việc đo lưu lượng
            camera_cycle["measurement_active"] = True
            
            # Khởi động lại chu trình camera
            camera_cycle["active"] = True
            camera_cycle["current_cycle_step"] = 0
            camera_cycle["last_position_change"] = current_time
            
            # Reset lại các biến đếm xe để bắt đầu đợt đếm mới
            vehicle_counting_data = {
                "cycle_counts": {
                    "direction1": [0, 0], # Reset đếm số xe cho hướng 1
                    "direction2": [0, 0]  # Reset đếm số xe cho hướng 2
                },
                "cycle_ids": {
                    "direction1": [set(), set()], # Reset IDs đã đếm cho hướng 1
                    "direction2": [set(), set()]  # Reset IDs đã đếm cho hướng 2
                },
                "current_cycle_index": 0
            }
            
            # Reset biến đếm tích lũy
            for direction_key in ["direction1", "direction2"]:
                cumulative_counts[direction_key]["total_vehicles"] = 0
                cumulative_counts[direction_key]["counted_ids"] = set()
            
            # Reset bộ đếm trong detector
            if detector:
                detector.reset_counter()
            
            # Kiểm tra xem đã có cấu hình mới chưa
            if cycle_control["next_session_config"]["ready"]:
                # Áp dụng cấu hình mới cho phiên tiếp theo
                traffic_control_settings["direction1_green_time"] = cycle_control["next_session_config"]["direction1_green_time"]
                traffic_control_settings["direction2_green_time"] = cycle_control["next_session_config"]["direction2_green_time"]
                
                # Reset trạng thái ready
                cycle_control["next_session_config"]["ready"] = False
                
                logger.info(f"Áp dụng cấu hình mới cho phiên tiếp theo: Làn 1 = {traffic_control_settings['direction1_green_time']}s, Làn 2 = {traffic_control_settings['direction2_green_time']}s")
            
            logger.info("Kết thúc thời gian delay, tiếp tục hoạt động bình thường với biến đếm đã reset")
            return True
        
        return False
    
    return True  # Không trong chế độ delay


# Tạo cấu trúc cho API
class CameraSettings(BaseModel):
    ip: str
    port: int = 554
    onvif_port: int = 80
    username: str
    password: str
    channel: int = 1
    stream: int = 1

class TrafficControlSettings(BaseModel):
    min_green_time: int
    max_green_time: int
    yellow_time: int
    direction1_green_time: Optional[int]
    direction2_green_time: Optional[int]

class CameraDirectionSettings(BaseModel):
    direction: int  # 0 or 1

class CameraCycleSettings(BaseModel):
    fixed_position_time: int
    active: bool

# Khởi tạo WebSocket manager
ws_manager = WebSocketManager()

# Lifespan context manager cho FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Khởi tạo các thành phần
    init_system()
    
    # Khởi động các background threads
    start_background_threads()
    
    # Thêm vào: Khởi chạy task xử lý frame async
    process_frame_task = asyncio.create_task(process_frame_async())
    
    yield
    
    # Cleanup khi shutdown
    global running
    running = False
    process_frame_task.cancel()  # Hủy task khi shutdown
    shutdown_system()

# Khởi tạo FastAPI app
app = FastAPI(title="Smart Traffic System API", lifespan=lifespan)

# Thêm CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Cho phép tất cả nguồn gốc
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def init_system():
    """Khởi tạo các thành phần hệ thống"""
    global ptz_controller, detector, cap
    
    # Thiết lập các hướng mặc định
    direction_setup["initialized"] = True
    direction_setup["user_defined"] = True
    direction_setup["direction1_angle"] = -0.55  # Vị trí 1 (góc -55 độ)
    direction_setup["direction2_angle"] = 0.55   # Vị trí 2 (góc +55 độ), tổng góc quay ~110 độ
    
    # Khởi tạo detector
    detector = YOLOv8Detector(model_path=DEFAULT_YOLO_MODEL)
    
    # Khởi tạo PTZ controller
    ptz_controller = ImouCameraPTZ(DEFAULT_IP, DEFAULT_ONVIF_PORT, DEFAULT_USERNAME, DEFAULT_PASSWORD)
    
    # Thiết lập vị trí chính xác
    ptz_controller.exact_positions[0] = {'pan': -0.55, 'tilt': 0.0}  # Vị trí 1
    ptz_controller.exact_positions[1] = {'pan': 0.55, 'tilt': 0.0}   # Vị trí 2
    
    # Kết nối camera
    connect_camera()
    
    logger.info("Hệ thống đã được khởi tạo")

def connect_camera():
    """Kết nối đến camera"""
    global cap
    
    try:
        # Nếu đã kết nối, giải phóng trước
        if cap is not None:
            cap.release()
            
        # Kết nối mới với thiết lập giảm độ trễ
        rtsp_url = f"rtsp://{DEFAULT_USERNAME}:{DEFAULT_PASSWORD}@{DEFAULT_IP}:{DEFAULT_PORT}/cam/realmonitor?channel={DEFAULT_CHANNEL}&subtype={DEFAULT_STREAM}"
        logger.info(f"Đang kết nối đến camera: {rtsp_url}")
        
        # Thiết lập FFMPEG để giảm độ trễ nhưng vẫn giữ chất lượng
        # Giảm buffer_size để giảm độ trễ, nhưng không quá nhỏ
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp|buffer_size;131072|max_delay;500000"
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        
        # Thiết lập buffer size nhỏ để giảm độ trễ
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        if not cap.isOpened():
            logger.error("Không thể kết nối đến camera!")
            # Sử dụng camera mặc định nếu không kết nối được
            cap = cv2.VideoCapture(0)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            
            if not cap.isOpened():
                logger.error("Không thể sử dụng camera mặc định!")
                return False
                
        logger.info("Đã kết nối đến camera thành công")
        return True
    except Exception as e:
        logger.error(f"Lỗi khi kết nối camera: {e}")
        return False

async def process_frame_async():
    """Xử lý frame từ camera và gửi đến client qua WebSocket"""
    global running, current_direction, latency_stats
    
    frame_count = 0
    frame_skip = 0  # Không bỏ qua frame
    last_stats_update = time.time()
    stats_interval = 1  # Giảm khoảng thời gian giữa các lần cập nhật thống kê xuống 1 giây
    
    while running:
        try:
            if cap is None or not cap.isOpened():
                logger.warning("Camera đã ngắt kết nối, đang thử kết nối lại...")
                connect_camera()
                await asyncio.sleep(0.1)
                continue
            
            # Bắt đầu đo thời gian tổng
            t_start_total = perf_counter()
            
            # Đo thời gian đọc frame
            t_start_read = perf_counter()
            ret, frame = cap.read()
            t_frame_read = perf_counter() - t_start_read
            latency_stats["frame_acquisition"].append(t_frame_read)
            
            if not ret:
                logger.warning("Không đọc được frame từ camera")
                await asyncio.sleep(0.01)
                continue
                
            # Ghi lại timestamp của frame gốc
            frame_timestamp = time.time()
            
            # Đếm frame để bỏ qua
            frame_count += 1
            if frame_count % (frame_skip + 1) != 0:
                await asyncio.sleep(0.001)
                continue
                
            # Xử lý frame nếu có kết nối WebSocket
            if ws_manager.active_connections:
                # Đo thời gian xử lý
                t_start_process = perf_counter()
                
                # Giữ nguyên độ phân giải gốc để đảm bảo chất lượng
                # Chỉ resize nếu frame quá lớn (trên 1280x720)
                h, w = frame.shape[:2]
                if w > 1280 or h > 720:
                    ratio = min(1280 / w, 720 / h)
                    small_frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
                else:
                    small_frame = frame
                
                # Xử lý phát hiện và đếm phương tiện
                processed_frame, vehicles, vehicle_detections, density, vehicle_types = detector.detect(small_frame)
                
                t_process = perf_counter() - t_start_process
                latency_stats["processing"].append(t_process)
                
                if camera_cycle["measurement_active"] and not cycle_control["in_delay_mode"]:
                    # Cập nhật thống kê chỉ khi không trong thời gian delay
                    update_traffic_stats(current_direction, vehicles, vehicle_detections, density, vehicle_types)
                else:
                    # Vẫn hiển thị kết quả phát hiện nhưng không cập nhật thống kê
                    logger.debug("Không cập nhật thống kê do đang trong chế độ delay hoặc không đo lường")
                
                # Đo thời gian mã hóa - Sử dụng chất lượng JPEG cao hơn (70)
                t_start_encode = perf_counter()
                _, encoded_frame = cv2.imencode('.jpg', processed_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                encoded_frame_str = base64.b64encode(encoded_frame).decode('utf-8')
                t_encode = perf_counter() - t_start_encode
                latency_stats["encoding"].append(t_encode)
                
                # Đo thời gian gửi qua WebSocket
                t_start_send = perf_counter()
                await ws_manager.broadcast({
                    "type": "frame", 
                    "data": encoded_frame_str, 
                    "timestamp": frame_timestamp  # Sử dụng timestamp của frame gốc
                })
                t_send = perf_counter() - t_start_send
                latency_stats["websocket_send"].append(t_send)
                
                # Tổng thời gian xử lý trong backend
                t_total = perf_counter() - t_start_total
                latency_stats["total"].append(t_total)
                
                # Tính độ trễ từ RTSP đến xử lý (RTSP internal buffering)
                t_rtsp_to_processing = frame_timestamp - (time.time() - t_total)
                latency_stats["rtsp_to_processing"].append(t_rtsp_to_processing)
                
                # Tăng số mẫu
                latency_stats["samples"] += 1
                
                # Cập nhật và gửi thống kê thường xuyên hơn
                current_time = time.time()
                if current_time - last_stats_update >= stats_interval:
                    update_traffic_lights()
                    await ws_manager.send_stats()
                    
                    last_stats_update = current_time
            
            # Đợi một khoảng thời gian ngắn - đủ để xử lý nhưng không tạo độ trễ
            await asyncio.sleep(0.005)
            
        except Exception as e:
            logger.error(f"Lỗi khi xử lý frame: {e}")
            await asyncio.sleep(0.1)

def process_frame_thread_function():
    """Thread xử lý frame"""
    global running, current_direction
    
    while running:
        try:
            if cap is None or not cap.isOpened():
                logger.warning("Camera đã ngắt kết nối, đang thử kết nối lại...")
                connect_camera()
                time.sleep(1)
                continue
                
            # Đọc frame từ camera
            ret, frame = cap.read()
            if not ret:
                logger.warning("Không đọc được frame từ camera")
                time.sleep(0.1)
                continue
                
            # Chỉ xử lý nếu hàng đợi không đầy
            if not process_queue.full():
                process_queue.put((frame.copy(), current_direction))
                
            time.sleep(0.01)
                
        except Exception as e:
            logger.error(f"Lỗi trong thread đọc frame: {e}")
            time.sleep(0.5)

def process_results_thread_function():
    """Thread xử lý kết quả và cập nhật thông số"""
    global running
    
    last_stats_update = time.time()
    
    while running:
        try:
            # Xử lý kết quả nếu có
            if not results_queue.empty():
                processed_frame, direction, vehicles, density = results_queue.get()
                results_queue.task_done()
                
                # Gửi kết quả qua WebSocket nếu có kết nối
                if ws_manager.active_connections:
                    asyncio.run(ws_manager.send_frame(processed_frame))
                    
            # Cập nhật và gửi thống kê định kỳ
            current_time = time.time()
            if current_time - last_stats_update >= 1.0:
                update_traffic_lights()
                check_delay_status()
                if ws_manager.active_connections:
                    asyncio.run(ws_manager.send_stats())
                last_stats_update = current_time
                
            time.sleep(0.01)
                
        except Exception as e:
            logger.error(f"Lỗi trong thread xử lý kết quả: {e}")
            time.sleep(0.5)

def camera_control_thread_function():
    """Thread điều khiển camera"""
    global running, current_direction, ptz_controller, camera_move_lock
    global camera_cycle, cycle_control, cumulative_counts
    
    while running:
        try:
            current_time = time.time()
            
            # Cập nhật thời gian còn lại nếu đang trong chế độ delay
            if cycle_control["in_delay_mode"]:
                update_delay_remaining()
                
                # Kiểm tra xem đã kết thúc delay chưa
                if current_time >= cycle_control["delay_end_time"]:
                    # Kết thúc chế độ delay
                    cycle_control["in_delay_mode"] = False
                    cycle_control["current_phase"] = "measuring"
                    camera_cycle["active"] = True
                    camera_cycle["measurement_active"] = True
                    camera_cycle["current_cycle_step"] = 0
                    camera_cycle["current_cycle_count"] = 0
                    # Thêm đoạn này vào hàm camera_control_thread_function trước dòng camera_cycle["last_position_change"] = current_time
                    # để theo dõi quá trình đếm
                    if camera_cycle["current_cycle_step"] == 0:
                        # Bắt đầu chu kỳ mới (hướng 1, lần 1)
                        logger.info("Bắt đầu chu kỳ đếm mới - Hướng 1, lần 1")
                    elif camera_cycle["current_cycle_step"] == 1:
                        # Hướng 2, lần 1
                        d1_count = vehicle_counting_data["cycle_counts"]["direction1"][0]
                        logger.info(f"Đã đếm xong hướng 1, lần 1: {d1_count} xe")
                    elif camera_cycle["current_cycle_step"] == 2:
                        # Hướng 1, lần 2
                        d2_count = vehicle_counting_data["cycle_counts"]["direction2"][0]
                        logger.info(f"Đã đếm xong hướng 2, lần 1: {d2_count} xe")
                    elif camera_cycle["current_cycle_step"] == 3:
                        # Hướng 2, lần 2 (kết thúc chu kỳ)
                        d1_count = vehicle_counting_data["cycle_counts"]["direction1"][1]
                        logger.info(f"Đã đếm xong hướng 1, lần 2: {d1_count} xe")
                    camera_cycle["last_position_change"] = current_time
                    
                    logger.info("Kết thúc delay, bắt đầu chu kỳ đo lường mới")
                    
                    # Di chuyển camera về vị trí ban đầu
                    with camera_move_lock:
                        if ptz_controller and ptz_controller.connected:
                            success = ptz_controller.move_to_user_defined_direction(0)
                            if success:
                                current_direction = 0
                                logger.info("Đã di chuyển camera về vị trí ban đầu (hướng 1)")
                
                # Vẫn đang trong chế độ delay, chờ
                time.sleep(1)
                continue
            
            # Chỉ xử lý nếu camera đang hoạt động và đã kết nối
            if camera_cycle["active"] and ptz_controller and ptz_controller.connected:
                # Kiểm tra xem đã đến lúc chuyển vị trí chưa
                if current_time - camera_cycle["last_position_change"] >= camera_cycle["fixed_position_time"]:
                    with camera_move_lock:
                        # Lưu số xe đếm được ở vị trí hiện tại
                        direction_key = f"direction{current_direction+1}"
                        total_vehicles = cumulative_counts[direction_key]["total_vehicles"]
                        
                        # Cập nhật tổng số xe cho vị trí hiện tại
                        update_total_vehicle_count(current_direction, total_vehicles)
                        
                        # Reset số xe đã đếm cho vị trí hiện tại
                        cumulative_counts[direction_key]["total_vehicles"] = 0
                        cumulative_counts[direction_key]["counted_ids"] = set()
                        
                        # Xác định hướng tiếp theo dựa vào chu trình
                        # Luân phiên giữa hướng 0 và 1
                        target_direction = 1 if current_direction == 0 else 0
                        
                        logger.info(f"Chu trình camera: Bước {camera_cycle['current_cycle_step']+1}/{camera_cycle['total_cycles']}, chuyển từ hướng {current_direction+1} sang hướng {target_direction+1}")
                        
                        # Di chuyển camera
                        success = ptz_controller.move_to_user_defined_direction(target_direction)
                            
                        if success:
                            current_direction = target_direction
                            
                            # Cập nhật bước trong chu trình
                            camera_cycle["current_cycle_step"] += 1
                            camera_cycle["current_cycle_count"] += 1
                            
                            # Kiểm tra xem đã hoàn thành chu trình chưa
                            if camera_cycle["current_cycle_count"] >= camera_cycle["total_cycles"]:
                                # Đã hoàn thành chu trình đo lường
                                logger.info("Hoàn thành chu trình đo lường! Tính toán thời gian đèn xanh mới.")
                                
                                # Chuyển sang pha tính toán
                                cycle_control["current_phase"] = "calculating"
                                
                                # Tính toán thời gian đèn xanh mới
                                direction1_green, direction2_green = calculate_traffic_light_times()
                                
                                # Cập nhật cài đặt đèn giao thông
                                traffic_control_settings["direction1_green_time"] = direction1_green
                                traffic_control_settings["direction2_green_time"] = direction2_green
                                
                                # Bắt đầu pha delay
                                start_delay_phase()
                        else:
                            logger.error(f"Không thể chuyển camera sang vị trí {target_direction+1}")
                        
                        # Cập nhật thời điểm chuyển vị trí
                        camera_cycle["last_position_change"] = current_time
            
            # Đợi 1 giây trước khi kiểm tra lại
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Lỗi trong thread điều khiển camera: {e}")
            time.sleep(5)

def start_background_threads():
    """Khởi động các background threads"""
    global running
    
    running = True
    
    # Khởi động thread xử lý frame
    process_thread = threading.Thread(target=process_frame_thread, args=(detector,))
    process_thread.daemon = True
    process_thread.start()
    
    # Khởi động thread điều khiển camera
    if ptz_controller and ptz_controller.connect_onvif():
        camera_thread = threading.Thread(target=camera_control_thread_function)
        camera_thread.daemon = True
        camera_thread.start()
        
        # Di chuyển camera đến vị trí ban đầu
        with camera_move_lock:
            if direction_setup["user_defined"]:
                logger.info("Di chuyển camera về hướng 1 đã định nghĩa để bắt đầu chu trình")
                ptz_controller.move_to_user_defined_direction(0)
            current_direction = 0
            camera_cycle["last_position_change"] = time.time()

def shutdown_system():
    """Tắt hệ thống và giải phóng tài nguyên"""
    global running, cap
    
    running = False
    
    # Giải phóng camera
    if cap:
        cap.release()
    
    # Dừng camera nếu đang di chuyển
    if ptz_controller and ptz_controller.connected:
        ptz_controller.stop_move()
    
    logger.info("Hệ thống đã được shutdown")

def process_frame_thread(detector):
    """Thread xử lý frame trong hàng đợi"""
    global running, traffic_stats, current_direction, camera_cycle
    
    while running:
        try:
            if not process_queue.empty():
                frame, direction = process_queue.get(timeout=1)
                
                # Đợi camera ổn định trước khi xử lý
                if not camera_at_position.is_set():
                    logger.info("Đợi camera ổn định trước khi xử lý frame")
                    camera_at_position.wait(timeout=3.0)
                
                # Xử lý phát hiện và đếm phương tiện
                processed_frame, vehicles, vehicle_detections, density, vehicle_types = detector.detect(frame)
                
                # Chỉ cập nhật thống kê nếu đang trong giai đoạn đo lưu lượng
                if camera_cycle["measurement_active"]:
                    # Cập nhật thống kê
                    update_traffic_stats(direction, vehicles, vehicle_detections, density, vehicle_types)
                else:
                    # Vẫn hiển thị kết quả phát hiện nhưng không cập nhật thống kê
                    logger.debug("Không cập nhật thống kê do đang trong chế độ delay")
                
                # Đưa kết quả vào hàng đợi (nếu hàng đợi không đầy)
                try:
                    results_queue.put((processed_frame, direction, vehicles, density), block=False)
                except queue.Full:
                    # Nếu hàng đợi đầy, loại bỏ một phần tử cũ nhất
                    try:
                        results_queue.get(block=False)
                        results_queue.put((processed_frame, direction, vehicles, density), block=False)
                    except:
                        pass
                
                process_queue.task_done()
            else:
                time.sleep(0.01)
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Lỗi trong thread xử lý: {str(e)}")

def send_data_to_thingspeak(green_time_direction1, red_time_direction1):
    """
    Gửi dữ liệu lên ThingSpeak
    
    Args:
        green_time_direction1: Thời gian đèn xanh của hướng 1 (giây)
        red_time_direction1: Thời gian đèn đỏ của hướng 1 (giây)
    
    Returns:
        bool: True nếu gửi thành công, False nếu thất bại
    """
    try:
        # Tạo payload với 2 trường dữ liệu (field1, field2)
        payload = {
            'api_key': THINGSPEAK_WRITE_API_KEY,
            'field1': green_time_direction1,
            'field2': red_time_direction1
        }
        
        # Gửi dữ liệu lên ThingSpeak qua HTTP request
        response = requests.post(THINGSPEAK_URL, data=payload)
        
        if response.status_code == 200:
            logger.info(f"Đã gửi dữ liệu thành công lên ThingSpeak: Đèn xanh hướng 1 = {green_time_direction1}s, Đèn đỏ hướng 1 = {red_time_direction1}s")
            return True
        else:
            logger.error(f"Lỗi khi gửi dữ liệu lên ThingSpeak: HTTP {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Lỗi khi gửi dữ liệu lên ThingSpeak: {e}")
        return False

# API Endpoints
@app.get("/")
async def root():
    return {"message": "Hệ thống giám sát giao thông thông minh API"}

@app.get("/status")
async def get_status():
    global traffic_stats, traffic_lights, camera_cycle, cycle_control, current_direction, ptz_controller
    
    camera_connected = cap is not None and cap.isOpened()
    ptz_connected = ptz_controller is not None and ptz_controller.connected
    
    return {
        "status": "running" if running else "stopped",
        "camera_connected": camera_connected,
        "ptz_connected": ptz_connected,
        "current_direction": current_direction + 1,
        "traffic_stats": traffic_stats,
        "traffic_lights": traffic_lights,
        "camera_cycle": camera_cycle,
        "cycle_control": cycle_control
    }

@app.post("/camera/connect")
async def set_camera_connection(camera_settings: CameraSettings):
    global DEFAULT_IP, DEFAULT_PORT, DEFAULT_ONVIF_PORT, DEFAULT_USERNAME, DEFAULT_PASSWORD
    
    # Cập nhật thông tin kết nối
    DEFAULT_IP = camera_settings.ip
    DEFAULT_PORT = camera_settings.port
    DEFAULT_ONVIF_PORT = camera_settings.onvif_port
    DEFAULT_USERNAME = camera_settings.username
    DEFAULT_PASSWORD = camera_settings.password
    
    # Kết nối camera
    if connect_camera():
        # Khởi tạo lại PTZ controller
        global ptz_controller
        ptz_controller = ImouCameraPTZ(DEFAULT_IP, DEFAULT_ONVIF_PORT, DEFAULT_USERNAME, DEFAULT_PASSWORD)
        ptz_controller.connect_onvif()
        
        return {"status": "success", "message": "Đã kết nối camera thành công"}
    else:
        raise HTTPException(status_code=500, detail="Không thể kết nối đến camera")

@app.post("/camera/direction")
async def set_camera_direction(direction_settings: CameraDirectionSettings):
    global current_direction, ptz_controller, camera_move_lock
    
    if ptz_controller is None or not ptz_controller.connected:
        raise HTTPException(status_code=400, detail="Camera PTZ chưa được kết nối")
    
    with camera_move_lock:
        direction = direction_settings.direction
        if direction not in [0, 1]:
            raise HTTPException(status_code=400, detail="Hướng không hợp lệ. Sử dụng 0 hoặc 1")
        
        # Di chuyển camera đến hướng mới
        if direction_setup["user_defined"]:
            success = ptz_controller.move_to_user_defined_direction(direction)
        else:
            success = ptz_controller.move_to_direction(direction)
        
        if success:
            current_direction = direction
            return {"status": "success", "message": f"Đã chuyển camera sang hướng {direction + 1}"}
        else:
            raise HTTPException(status_code=500, detail=f"Không thể chuyển camera sang hướng {direction + 1}")

@app.post("/camera/cycle/settings")
async def set_camera_cycle(cycle_settings: CameraCycleSettings):
    global camera_cycle
    
    # Cập nhật cài đặt chu trình
    camera_cycle["fixed_position_time"] = cycle_settings.fixed_position_time
    camera_cycle["active"] = cycle_settings.active
    
    return {"status": "success", "message": "Đã cập nhật cài đặt chu trình camera"}

@app.post("/traffic/settings")
async def set_traffic_settings(settings: TrafficControlSettings):
    global traffic_control_settings
    
    # Cập nhật cài đặt điều khiển giao thông
    traffic_control_settings["min_green_time"] = settings.min_green_time
    traffic_control_settings["max_green_time"] = settings.max_green_time
    traffic_control_settings["yellow_time"] = settings.yellow_time
    
    if settings.direction1_green_time is not None:
        traffic_control_settings["direction1_green_time"] = settings.direction1_green_time
    
    if settings.direction2_green_time is not None:
        traffic_control_settings["direction2_green_time"] = settings.direction2_green_time
    
    return {"status": "success", "message": "Đã cập nhật cài đặt điều khiển giao thông"}

@app.post("/traffic/reset")
async def reset_traffic():
    # Reset thống kê giao thông
    reset_traffic_stats()
    return {"status": "success", "message": "Đã reset thống kê giao thông"}

@app.get("/traffic/stats")
async def get_traffic_stats():
    return {
        "traffic_stats": traffic_stats,
        "traffic_lights": traffic_lights
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global current_direction
    await ws_manager.connect(websocket)
    try:
        # Gửi thông báo kết nối thành công và trạng thái hiện tại
        await websocket.send_json({"type": "response", "status": "success", "message": "Kết nối WebSocket thành công"})
        
        vehicle_counting_data_json = {
            "cycle_counts": vehicle_counting_data["cycle_counts"],
            "current_cycle_index": vehicle_counting_data["current_cycle_index"],
            "cycle_ids": {
                "direction1": [list(s) for s in vehicle_counting_data["cycle_ids"]["direction1"]],
                "direction2": [list(s) for s in vehicle_counting_data["cycle_ids"]["direction2"]]
            }
        }
        
        # Gửi trạng thái hiện tại
        stats_data = {
            "type": "stats",
            "data": {
                "traffic_stats": traffic_stats,
                "traffic_lights": traffic_lights,
                "camera_cycle": camera_cycle,
                "cycle_control": cycle_control,
                "current_direction": current_direction,
                # Sử dụng phiên bản đã chuyển đổi
                "vehicle_counting_data": vehicle_counting_data_json
            }
        }
        await websocket.send_json(stats_data)
        
        # Đợi các lệnh khác từ client
        while True:
            try:
                data = await websocket.receive_text()
                command = json.loads(data)
                logger.info(f"Nhận được lệnh từ client: {command}")
                
                # Xử lý lệnh điều khiển camera theo góc
                if command.get("type") == "camera_control":
                    action = command.get("action", "")
                    if action == "left":
                        # Di chuyển camera sang trái
                        with camera_move_lock:
                            if ptz_controller and ptz_controller.connected:
                                success = ptz_controller.move_continuous(-0.5, 0, 2.0)  # Quay trái
                                logger.info("Camera di chuyển sang trái")
                                current_direction = 0  # Đặt thành hướng 1 sau khi di chuyển
                                await websocket.send_json({"type": "response", "status": "success", "message": "Đã di chuyển camera sang trái"})
                            else:
                                await websocket.send_json({"type": "response", "status": "error", "message": "Không thể điều khiển camera"})
                    
                    elif action == "right":
                        # Di chuyển camera sang phải
                        with camera_move_lock:
                            if ptz_controller and ptz_controller.connected:
                                success = ptz_controller.move_continuous(0.5, 0, 2.0)  # Quay phải
                                logger.info("Camera di chuyển sang phải")
                                current_direction = 1  # Đặt thành hướng 2 sau khi di chuyển
                                await websocket.send_json({"type": "response", "status": "success", "message": "Đã di chuyển camera sang phải"})
                            else:
                                await websocket.send_json({"type": "response", "status": "error", "message": "Không thể điều khiển camera"})
                
                # Xử lý các lệnh khác
                elif command.get("type") == "camera_direction":
                    camera_cycle["active"] = bool(command.get("active", True))
                    if "fixed_position_time" in command:
                        camera_cycle["fixed_position_time"] = int(command["fixed_position_time"])
                    await websocket.send_json({"type": "response", "status": "success", "message": "Đã cập nhật cài đặt chu trình camera"})
                
            except Exception as e:
                logger.error(f"Lỗi khi xử lý lệnh: {e}")
                break
                
    except WebSocketDisconnect:
        logger.info("Client WebSocket ngắt kết nối")
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"Lỗi WebSocket: {e}")
        ws_manager.disconnect(websocket)


@app.get("/api/heartbeat")
async def heartbeat():
    return {"status": "ok", "message": "Server đang hoạt động", "timestamp": time.time()}

    
@app.get("/api/cycle-info")
async def get_cycle_info():
    """Endpoint để lấy thông tin về chu trình hiện tại"""
    return {
        "camera_cycle": {
            "current_cycle_step": camera_cycle["current_cycle_step"],
            "total_cycles": camera_cycle["total_cycles"],
            "active": camera_cycle["active"],
            "direction_counts": camera_cycle["direction_counts"]
        },
        "cycle_control": {
            "current_phase": cycle_control["current_phase"],
            "in_delay_mode": cycle_control["in_delay_mode"],
            "delay_remaining": update_delay_remaining(),
            "next_config": {
                "direction1_green_time": cycle_control["next_session_config"]["direction1_green_time"],
                "direction2_green_time": cycle_control["next_session_config"]["direction2_green_time"]
            }
        },
        "traffic_settings": {
            "direction1_green_time": traffic_control_settings["direction1_green_time"],
            "direction2_green_time": traffic_control_settings["direction2_green_time"]
        }
    }
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)