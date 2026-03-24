import eventlet
eventlet.monkey_patch()

from eventlet.green import socket
import struct
import base64
import io
import logging
import time

logger = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:
    logger.error("Pillow not installed")
    Image = None


class VNCClient:
    def __init__(self, host='127.0.0.1', port=5900):
        self.host = host
        self.port = port
        self.sock = None
        self.width = 640
        self.height = 480
        self.connected = False
        self.framebuffer = None
        self.update_greenlet = None
        self._lock = eventlet.semaphore.Semaphore()
    
    def connect(self):
        """Connect to VNC server"""
        if Image is None:
            logger.error("PIL not available")
            return False
        
        try:
            logger.info(f"Connecting to VNC {self.host}:{self.port}")
            
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((self.host, self.port))
            
            # Protocol version
            server_version = self._recv(12)
            logger.info(f"Server version: {server_version}")
            
            # Use RFB 3.3 for simplicity
            self.sock.sendall(b'RFB 003.003\n')
            
            # Security type (3.3 sends it as uint32)
            sec_type = struct.unpack('>I', self._recv(4))[0]
            logger.info(f"Security type: {sec_type}")
            
            if sec_type == 0:
                # Error
                err_len = struct.unpack('>I', self._recv(4))[0]
                err_msg = self._recv(err_len)
                logger.error(f"VNC error: {err_msg}")
                return False
            elif sec_type == 1:
                # None - no auth needed
                pass
            elif sec_type == 2:
                # VNC auth - not implemented
                logger.error("VNC auth not supported")
                return False
            
            # ClientInit - shared flag
            self.sock.sendall(struct.pack('B', 1))
            
            # ServerInit
            self.width = struct.unpack('>H', self._recv(2))[0]
            self.height = struct.unpack('>H', self._recv(2))[0]
            
            # Pixel format (16 bytes)
            pf = self._recv(16)
            bpp, depth, be, tc = struct.unpack('>BBBB', pf[0:4])
            logger.info(f"Pixel format: {bpp}bpp, depth={depth}, be={be}, tc={tc}")
            
            # Desktop name
            name_len = struct.unpack('>I', self._recv(4))[0]
            name = self._recv(name_len) if name_len > 0 else b''
            
            logger.info(f"Desktop: {self.width}x{self.height} '{name.decode(errors='ignore')}'")
            
            # Set pixel format (32-bit BGRX)
            self._set_pixel_format()
            
            # Set encodings
            self._set_encodings()
            
            # Create framebuffer
            self.framebuffer = Image.new('RGB', (self.width, self.height), (0, 0, 0))
            
            self.connected = True
            self.sock.settimeout(2)
            
            # Start update loop
            self.update_greenlet = eventlet.spawn(self._update_loop)
            
            logger.info("VNC connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"VNC connect failed: {e}", exc_info=True)
            self.disconnect()
            return False
    
    def _recv(self, n):
        """Receive exactly n bytes"""
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Connection closed")
            data += chunk
        return data
    
    def _send(self, data):
        """Thread-safe send"""
        with self._lock:
            try:
                self.sock.sendall(data)
                return True
            except Exception as e:
                logger.error(f"Send error: {e}")
                return False
    
    def _set_pixel_format(self):
        """Set 32-bit pixel format"""
        msg = struct.pack('>B', 0)  # SetPixelFormat message type
        msg += b'\x00\x00\x00'      # Padding
        
        # Pixel format
        msg += struct.pack('B', 32)   # bits-per-pixel
        msg += struct.pack('B', 24)   # depth
        msg += struct.pack('B', 0)    # big-endian-flag
        msg += struct.pack('B', 1)    # true-colour-flag
        msg += struct.pack('>H', 255) # red-max
        msg += struct.pack('>H', 255) # green-max
        msg += struct.pack('>H', 255) # blue-max
        msg += struct.pack('B', 16)   # red-shift
        msg += struct.pack('B', 8)    # green-shift
        msg += struct.pack('B', 0)    # blue-shift
        msg += b'\x00\x00\x00'        # Padding
        
        self._send(msg)
    
    def _set_encodings(self):
        """Set supported encodings"""
        encodings = [0]  # Raw only
        
        msg = struct.pack('>B', 2)     # SetEncodings message type
        msg += b'\x00'                  # Padding
        msg += struct.pack('>H', len(encodings))
        
        for enc in encodings:
            msg += struct.pack('>i', enc)
        
        self._send(msg)
    
    def _request_update(self, incremental=True):
        """Request framebuffer update"""
        msg = struct.pack('>B', 3)      # FramebufferUpdateRequest
        msg += struct.pack('B', 1 if incremental else 0)
        msg += struct.pack('>H', 0)     # x
        msg += struct.pack('>H', 0)     # y
        msg += struct.pack('>H', self.width)
        msg += struct.pack('>H', self.height)
        
        self._send(msg)
    
    def _update_loop(self):
        """Background loop to receive screen updates"""
        logger.info("VNC update loop started")
        
        # Request full update first
        self._request_update(False)
        
        while self.connected:
            try:
                # Try to read message
                try:
                    msg_type = struct.unpack('B', self.sock.recv(1))[0]
                except socket.timeout:
                    # No data, request update
                    self._request_update(True)
                    continue
                except:
                    break
                
                if msg_type == 0:  # FramebufferUpdate
                    self._handle_fb_update()
                elif msg_type == 1:  # SetColourMapEntries
                    self._skip_colormap()
                elif msg_type == 2:  # Bell
                    pass
                elif msg_type == 3:  # ServerCutText
                    self._skip_cut_text()
                else:
                    logger.warning(f"Unknown message type: {msg_type}")
                
                # Request next update
                self._request_update(True)
                
                eventlet.sleep(0.01)
                
            except socket.timeout:
                self._request_update(True)
            except Exception as e:
                if self.connected:
                    logger.error(f"Update loop error: {e}")
                break
        
        logger.info("VNC update loop ended")
    
    def _handle_fb_update(self):
        """Handle framebuffer update"""
        _ = self._recv(1)  # Padding
        num_rects = struct.unpack('>H', self._recv(2))[0]
        
        for _ in range(num_rects):
            x = struct.unpack('>H', self._recv(2))[0]
            y = struct.unpack('>H', self._recv(2))[0]
            w = struct.unpack('>H', self._recv(2))[0]
            h = struct.unpack('>H', self._recv(2))[0]
            enc = struct.unpack('>i', self._recv(4))[0]
            
            if enc == 0 and w > 0 and h > 0:  # Raw
                size = w * h * 4
                pixels = self._recv(size)
                
                try:
                    img = Image.frombytes('RGBX', (w, h), pixels, 'raw', 'BGRX')
                    self.framebuffer.paste(img.convert('RGB'), (x, y))
                except Exception as e:
                    logger.error(f"Decode error: {e}")
            elif enc == -223:  # DesktopSize
                self.width = w
                self.height = h
                self.framebuffer = Image.new('RGB', (w, h), (0, 0, 0))
                logger.info(f"Desktop resized: {w}x{h}")
    
    def _skip_colormap(self):
        """Skip colormap entries"""
        _ = self._recv(1)
        _ = self._recv(2)
        n = struct.unpack('>H', self._recv(2))[0]
        _ = self._recv(n * 6)
    
    def _skip_cut_text(self):
        """Skip cut text"""
        _ = self._recv(3)
        length = struct.unpack('>I', self._recv(4))[0]
        if length > 0:
            _ = self._recv(length)
    
    def disconnect(self):
        """Disconnect from server"""
        logger.info("Disconnecting VNC")
        self.connected = False
        
        if self.update_greenlet:
            try:
                self.update_greenlet.kill()
            except:
                pass
            self.update_greenlet = None
        
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
    
    def get_frame(self):
        """Get current frame as JPEG"""
        if not self.connected or not self.framebuffer:
            return None
        
        try:
            buf = io.BytesIO()
            self.framebuffer.save(buf, format='JPEG', quality=75)
            return {
                'width': self.width,
                'height': self.height,
                'data': base64.b64encode(buf.getvalue()).decode()
            }
        except:
            return None
    
    def send_key(self, keysym, down):
        """Send key event"""
        if not self.connected:
            return False
        
        msg = struct.pack('>B', 4)           # KeyEvent
        msg += struct.pack('B', 1 if down else 0)  # down-flag
        msg += struct.pack('>H', 0)          # Padding
        msg += struct.pack('>I', keysym)     # key
        
        return self._send(msg)
    
    def send_mouse(self, x, y, buttons):
        """Send mouse/pointer event"""
        if not self.connected:
            return False
        
        # Clamp coordinates
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        buttons = int(buttons) & 0xFF
        
        msg = struct.pack('>B', 5)           # PointerEvent
        msg += struct.pack('B', buttons)      # button-mask
        msg += struct.pack('>H', x)          # x-position
        msg += struct.pack('>H', y)          # y-position
        
        result = self._send(msg)
        
        if result:
            logger.debug(f"Mouse sent: ({x}, {y}) buttons={buttons}")
        
        return result 
