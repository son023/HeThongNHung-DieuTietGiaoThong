import React, { useState, useEffect, useRef } from 'react';
import './App.css';

function App() {
  // Giảm thiểu số lượng state để tránh re-render
  const [connected, setConnected] = useState(false);
  const [streamDelay, setStreamDelay] = useState(0);
  
  // Sử dụng refs thay vì state khi có thể
  const wsRef = useRef(null);
  const videoRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const systemStateRef = useRef({
    time: '--:--',
    current_direction: 1,
    traffic_stats: {
      direction1: { vehicles: 0, density: 0 },
      direction2: { vehicles: 0, density: 0 }
    },
    traffic_lights: {
      direction1: { state: 'red', time_left: 0 },
      direction2: { state: 'green', time_left: 0 }
    },
    camera_cycle: {
      current_cycle_step: 0,
      total_cycles: 4,
      active: true,
      direction_counts: {
        direction1: 0,
        direction2: 0
      }
    },
    cycle_control: {
      current_phase: "measuring",
      in_delay_mode: false,
      delay_remaining: 0,
      next_config: {
        direction1_green_time: 60,
        direction2_green_time: 60,
        ready: false
      }
    }
  });
  
  // DOM references cho cập nhật trực tiếp
  const timeDisplayRef = useRef(null);
  const direction1VehiclesRef = useRef(null);
  const direction1DensityRef = useRef(null);
  const direction2VehiclesRef = useRef(null);
  const direction2DensityRef = useRef(null);
  const cameraDirRef = useRef(null);
  const connectionStatusRef = useRef(null);
  const delayDisplayRef = useRef(null);
  const light1Refs = useRef({ red: null, yellow: null, green: null, time: null });
  const light2Refs = useRef({ red: null, yellow: null, green: null, time: null });
  
  // Thêm refs mới cho hiển thị thông tin mới
  const delayRemainingRef = useRef(null);
  const delayStatusRef = useRef(null);
  const totalVehiclesDir1Ref = useRef(null);
  const totalVehiclesDir2Ref = useRef(null);
  const nextGreenTimeDir1Ref = useRef(null);
  const nextGreenTimeDir2Ref = useRef(null);
  const currentPhaseRef = useRef(null);
  const cycleStepRef = useRef(null);
  
  // Tham chiếu đến frame hiện tại
  const frameQueueRef = useRef([]);
  const isRenderingRef = useRef(false);
  const lastRenderTimeRef = useRef(0);
  
  // Giới hạn tốc độ render (30fps = ~33ms)
  const RENDER_INTERVAL = 33; 
  // Giới hạn độ dài queue để tránh tích lũy độ trễ
  const MAX_QUEUE_LENGTH = 2;
  
  // Cập nhật giao diện từ references
  const updateUIFromRefs = () => {
    const state = systemStateRef.current;
    
    // Cập nhật thời gian
    if (timeDisplayRef.current) {
      timeDisplayRef.current.textContent = state.time;
    }
    
    // Cập nhật camera direction
    if (cameraDirRef.current) {
      cameraDirRef.current.textContent = `Hướng ${state.current_direction}`;
    }
    
    // Cập nhật thống kê
    if (direction1VehiclesRef.current) {
      direction1VehiclesRef.current.textContent = state.traffic_stats.direction1.vehicles;
    }
    if (direction1DensityRef.current) {
      direction1DensityRef.current.textContent = state.traffic_stats.direction1.density.toFixed(2);
    }
    if (direction2VehiclesRef.current) {
      direction2VehiclesRef.current.textContent = state.traffic_stats.direction2.vehicles;
    }
    if (direction2DensityRef.current) {
      direction2DensityRef.current.textContent = state.traffic_stats.direction2.density.toFixed(2);
    }
    
    // Cập nhật trạng thái kết nối
    if (connectionStatusRef.current) {
      connectionStatusRef.current.textContent = connected ? "Đã kết nối" : "Mất kết nối";
      connectionStatusRef.current.className = connected ? "connected" : "disconnected";
    }
    
    // Cập nhật độ trễ
    if (delayDisplayRef.current) {
      delayDisplayRef.current.textContent = `Độ trễ: ${(streamDelay / 1000).toFixed(1)}s`;
      delayDisplayRef.current.className = streamDelay > 3000 ? "high-delay" : 
                                       streamDelay > 1000 ? "medium-delay" : "low-delay";
    }
    
    // Cập nhật traffic light 1
    const light1 = state.traffic_lights.direction1;
    if (light1Refs.current.red && light1Refs.current.yellow && light1Refs.current.green) {
      light1Refs.current.red.style.backgroundColor = light1.state === 'red' ? '#ff0000' : '#333333';
      light1Refs.current.yellow.style.backgroundColor = light1.state === 'yellow' ? '#ffff00' : '#333333';
      light1Refs.current.green.style.backgroundColor = light1.state === 'green' ? '#00ff00' : '#333333';
      
      if (light1Refs.current.time) {
        light1Refs.current.time.textContent = `${light1.time_left}s`;
      }
    }
    
    // Cập nhật traffic light 2
    const light2 = state.traffic_lights.direction2;
    if (light2Refs.current.red && light2Refs.current.yellow && light2Refs.current.green) {
      light2Refs.current.red.style.backgroundColor = light2.state === 'red' ? '#ff0000' : '#333333';
      light2Refs.current.yellow.style.backgroundColor = light2.state === 'yellow' ? '#ffff00' : '#333333';
      light2Refs.current.green.style.backgroundColor = light2.state === 'green' ? '#00ff00' : '#333333';
      
      if (light2Refs.current.time) {
        light2Refs.current.time.textContent = `${light2.time_left}s`;
      }
    }
    
    // Cập nhật thông tin chu kỳ delay
    if (delayStatusRef.current) {
      delayStatusRef.current.textContent = state.cycle_control.in_delay_mode ? "Đang chờ" : "Đang đo";
      delayStatusRef.current.className = state.cycle_control.in_delay_mode ? "delay-active" : "delay-inactive";
    }
    
    // Cập nhật thời gian delay còn lại
    if (delayRemainingRef.current) {
      delayRemainingRef.current.textContent = state.cycle_control.in_delay_mode ? 
        `${state.cycle_control.delay_remaining}s` : "Không có delay";
    }
    
    // Cập nhật tổng số xe đếm được ở mỗi hướng
    if (totalVehiclesDir1Ref.current) {
      totalVehiclesDir1Ref.current.textContent = state.camera_cycle.direction_counts.direction1;
    }
    
    if (totalVehiclesDir2Ref.current) {
      totalVehiclesDir2Ref.current.textContent = state.camera_cycle.direction_counts.direction2;
    }
    
    // Cập nhật thời gian đèn xanh tiếp theo
    if (nextGreenTimeDir1Ref.current) {
      nextGreenTimeDir1Ref.current.textContent = 
        state.cycle_control.next_config.ready ? 
        `${state.cycle_control.next_config.direction1_green_time}s` : "Chờ tính toán";
    }
    
    if (nextGreenTimeDir2Ref.current) {
      nextGreenTimeDir2Ref.current.textContent = 
        state.cycle_control.next_config.ready ? 
        `${state.cycle_control.next_config.direction2_green_time}s` : "Chờ tính toán";
    }
    
    // Cập nhật pha hiện tại
    if (currentPhaseRef.current) {
      let phaseText = "";
      switch(state.cycle_control.current_phase) {
        case "measuring":
          phaseText = "Đang đo lưu lượng";
          break;
        case "calculating":
          phaseText = "Đang tính toán";
          break;
        case "delaying":
          phaseText = "Đang chờ chu kỳ mới";
          break;
        default:
          phaseText = state.cycle_control.current_phase;
      }
      currentPhaseRef.current.textContent = phaseText;
    }
    
    // Cập nhật bước chu kỳ
    if (cycleStepRef.current) {
      cycleStepRef.current.textContent = `${state.camera_cycle.current_cycle_step}/${state.camera_cycle.total_cycles}`;
    }
  };
  
  // Cơ chế render frames với giới hạn tốc độ và queue length
  const startFrameProcessor = () => {
    const processNextFrame = () => {
      if (isRenderingRef.current) return;
      
      // Kiểm tra thời gian từ lần render cuối
      const now = performance.now();
      const timeSinceLastRender = now - lastRenderTimeRef.current;
      
      if (timeSinceLastRender < RENDER_INTERVAL) {
        // Chưa đến thời gian render tiếp theo
        requestAnimationFrame(processNextFrame);
        return;
      }
      
      if (frameQueueRef.current.length === 0) {
        // Không có frame trong queue
        requestAnimationFrame(processNextFrame);
        return;
      }
      
      isRenderingRef.current = true;
      
      // Luôn lấy frame mới nhất từ queue để giảm độ trễ
      const frameData = frameQueueRef.current.pop();
      // Xóa các frame cũ
      frameQueueRef.current = [];
      
      if (videoRef.current && frameData) {
        videoRef.current.src = `data:image/jpeg;base64,${frameData.data}`;
        
        // Tính độ trễ
        if (frameData.timestamp) {
          const serverTime = frameData.timestamp * 1000;
          const clientTime = Date.now();
          setStreamDelay(clientTime - serverTime);
        }
      }
      
      lastRenderTimeRef.current = now;
      isRenderingRef.current = false;
      
      // Tiếp tục vòng lặp
      requestAnimationFrame(processNextFrame);
    };
    
    // Bắt đầu vòng lặp xử lý frame
    requestAnimationFrame(processNextFrame);
  };
  
  // Kết nối WebSocket và xử lý messages
  useEffect(() => {
    const connectWebSocket = () => {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const host = window.location.hostname || 'localhost';
      const port = 8000;
      
      console.log(`Connecting to ${protocol}//${host}:${port}/ws`);
      
      const ws = new WebSocket(`${protocol}//${host}:${port}/ws`);
      
      ws.onopen = () => {
        console.log('WebSocket connected');
        setConnected(true);
        clearTimeout(reconnectTimerRef.current);
      };
      
      ws.onclose = () => {
        console.log('WebSocket disconnected');
        setConnected(false);
        reconnectTimerRef.current = setTimeout(connectWebSocket, 1000);
      };
      
      ws.onerror = (error) => {
        console.error('WebSocket error:', error);
      };
      
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          
          if (data.type === 'frame') {
            // Thêm frame mới vào queue, giới hạn kích thước queue
            if (frameQueueRef.current.length < MAX_QUEUE_LENGTH) {
              frameQueueRef.current.push(data);
            } else {
              // Nếu queue đầy, thay thế frame cũ nhất bằng frame mới nhất
              frameQueueRef.current = [data];
            }
          } else if (data.type === 'stats') {
            // Cập nhật data vào reference
            systemStateRef.current = {
              ...systemStateRef.current,
              current_direction: data.data.current_direction + 1,
              traffic_stats: data.data.traffic_stats,
              traffic_lights: data.data.traffic_lights,
              camera_cycle: data.data.camera_cycle,
              cycle_control: data.data.cycle_control,
              time: new Date().toLocaleTimeString()
            };
            
            // Cập nhật UI từ references
            updateUIFromRefs();
          }
        } catch (error) {
          console.error('Error parsing message:', error);
        }
      };
      
      wsRef.current = ws;
    };
    
    // Kết nối WebSocket
    connectWebSocket();
    
    // Khởi động trình xử lý frames
    startFrameProcessor();
    
    // Cleanup
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
    };
  }, []);
  
  // Hàm chuyển hướng camera
  const changeDirection = (direction) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: "camera_direction",
        direction: direction - 1 // UI: 1/2, Backend: 0/1
      }));
    }
  };
  
  // Hàm reset thống kê
  const resetStats = async () => {
    try {
      await fetch('/traffic/reset', {
        method: 'POST',
      });
    } catch (error) {
      console.error('Error resetting stats:', error);
    }
  };
  
  // Clear WebSocket Buffer
  const clearBuffer = () => {
    frameQueueRef.current = [];
    console.log("Đã xóa buffer WebSocket");
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Hệ Thống Giao Thông Thông Minh</h1>
        <div className="header-info">
          <p>Thời gian: <span ref={timeDisplayRef}>--:--</span></p>
          <p ref={delayDisplayRef} className="low-delay">Độ trễ: 0.0s</p>
          <button className="clear-buffer" onClick={clearBuffer}>Xóa Buffer</button>
        </div>
      </header>
      
      <main className="main">
        <div className="video-container">
          <h2>Camera Stream</h2>
          {connected ? (
            <img 
              ref={videoRef}
              src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
              alt="Traffic Camera Feed" 
              className="video-feed" 
            />
          ) : (
            <div className="connection-error">
              <p>Mất kết nối đến server</p>
              <p>Đang thử kết nối lại...</p>
            </div>
          )}
        </div>
        
        <div className="controls-container">
          <div className="status-panel">
            <h2>Trạng Thái Hệ Thống</h2>
            <div className="status-item">
              <span>Camera hiện tại:</span>
              <span ref={cameraDirRef}>Hướng 1</span>
            </div>
            <div className="status-item">
              <span>Trạng thái kết nối:</span>
              <span ref={connectionStatusRef} className={connected ? "connected" : "disconnected"}>
                {connected ? "Đã kết nối" : "Mất kết nối"}
              </span>
            </div>
            <div className="status-item">
              <span>Chu kỳ đo:</span>
              <span ref={cycleStepRef}>0/4</span>
            </div>
            <div className="status-item">
              <span>Pha hiện tại:</span>
              <span ref={currentPhaseRef}>Đang đo lưu lượng</span>
            </div>
            <div className="status-item">
              <span>Trạng thái delay:</span>
              <span ref={delayStatusRef} className="delay-inactive">Đang đo</span>
            </div>
            <div className="status-item">
              <span>Thời gian chờ:</span>
              <span ref={delayRemainingRef}>Không có delay</span>
            </div>
            <div className="button-container">
              <button onClick={() => changeDirection(1)}>
                Hướng 1
              </button>
              <button onClick={() => changeDirection(2)}>
                Hướng 2
              </button>
              <button className="reset-button" onClick={resetStats}>
                Reset
              </button>
            </div>
          </div>
          
          <div className="traffic-stats">
            <h2>Thống Kê Giao Thông</h2>
            <div className="stats-container">
              <div className="stats-column">
                <h3>Hướng 1</h3>
                <div className="stats-item">
                  <span>Xe hiện tại:</span>
                  <span ref={direction1VehiclesRef}>0</span>
                </div>
                <div className="stats-item">
                  <span>Mật độ:</span>
                  <span ref={direction1DensityRef}>0.00</span>
                </div>
                <div className="stats-item highlight">
                  <span>Tổng xe đếm được:</span>
                  <span ref={totalVehiclesDir1Ref}>0</span>
                </div>
                <div className="stats-item highlight">
                  <span>Đèn xanh tiếp theo:</span>
                  <span ref={nextGreenTimeDir1Ref}>Chờ tính toán</span>
                </div>
              </div>
              
              <div className="stats-column">
                <h3>Hướng 2</h3>
                <div className="stats-item">
                  <span>Xe hiện tại:</span>
                  <span ref={direction2VehiclesRef}>0</span>
                </div>
                <div className="stats-item">
                  <span>Mật độ:</span>
                  <span ref={direction2DensityRef}>0.00</span>
                </div>
                <div className="stats-item highlight">
                  <span>Tổng xe đếm được:</span>
                  <span ref={totalVehiclesDir2Ref}>0</span>
                </div>
                <div className="stats-item highlight">
                  <span>Đèn xanh tiếp theo:</span>
                  <span ref={nextGreenTimeDir2Ref}>Chờ tính toán</span>
                </div>
              </div>
            </div>
          </div>
          
          <div className="traffic-lights">
            <h2>Đèn Giao Thông</h2>
            <div className="lights-container">
              {/* Đèn giao thông 1 */}
              <div className="traffic-light">
                <div className="light-label">Hướng 1</div>
                <div className="light-container">
                  <div className="light" ref={el => light1Refs.current.red = el} style={{backgroundColor: '#333333'}}></div>
                  <div className="light" ref={el => light1Refs.current.yellow = el} style={{backgroundColor: '#333333'}}></div>
                  <div className="light" ref={el => light1Refs.current.green = el} style={{backgroundColor: '#333333'}}></div>
                </div>
                <div className="time-left" ref={el => light1Refs.current.time = el}>0s</div>
              </div>
              
              {/* Đèn giao thông 2 */}
              <div className="traffic-light">
                <div className="light-label">Hướng 2</div>
                <div className="light-container">
                  <div className="light" ref={el => light2Refs.current.red = el} style={{backgroundColor: '#333333'}}></div>
                  <div className="light" ref={el => light2Refs.current.yellow = el} style={{backgroundColor: '#333333'}}></div>
                  <div className="light" ref={el => light2Refs.current.green = el} style={{backgroundColor: '#333333'}}></div>
                </div>
                <div className="time-left" ref={el => light2Refs.current.time = el}>0s</div>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;