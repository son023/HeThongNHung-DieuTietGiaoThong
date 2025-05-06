import socket
import subprocess
import ipaddress
import platform
import threading
import time

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def get_network_prefix():
    ip = get_local_ip()
    # Lấy 3 octet đầu tiên của địa chỉ IP
    prefix = ".".join(ip.split(".")[:3])
    return prefix

def ping(host):
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    command = ['ping', param, '1', host]
    return subprocess.call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

def scan_port(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    result = sock.connect_ex((ip, port))
    sock.close()
    return result == 0

def check_device(ip):
    if ping(ip):
        # Kiểm tra các cổng phổ biến của Imou/IP cameras
        ports_to_check = [80, 554, 8000, 8080, 9000]
        open_ports = []
        for port in ports_to_check:
            if scan_port(ip, port):
                open_ports.append(port)
        
        if open_ports:
            print(f"Thiết bị tại {ip} có thể là camera với các cổng mở: {open_ports}")
            # Nếu cổng 80 mở, có thể thử kết nối HTTP để xác nhận
            if 80 in open_ports or 8080 in open_ports:
                port = 80 if 80 in open_ports else 8080
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    sock.connect((ip, port))
                    sock.send(b"GET / HTTP/1.1\r\nHost: " + ip.encode() + b"\r\n\r\n")
                    response = sock.recv(1024)
                    sock.close()
                    
                    # Kiểm tra các chuỗi phổ biến trong phản hồi của camera Imou
                    if b"imou" in response.lower() or b"lechange" in response.lower() or b"dahua" in response.lower():
                        print(f"Xác nhận {ip} là camera Imou/Lechange/Dahua!")
                except:
                    pass

def scan_network():
    prefix = get_network_prefix()
    threads = []
    
    print(f"Đang quét mạng {prefix}.0/24 để tìm camera Imou...")
    
    for i in range(1, 255):
        ip = f"{prefix}.{i}"
        t = threading.Thread(target=check_device, args=(ip,))
        threads.append(t)
        t.start()
        
        # Giới hạn số lượng thread đồng thời để tránh quá tải
        if len(threads) >= 20:
            for t in threads:
                t.join()
            threads = []
    
    # Chờ các thread còn lại hoàn thành
    for t in threads:
        t.join()

if __name__ == "__main__":
    print("Bắt đầu tìm kiếm camera Imou trên mạng...")
    scan_network()