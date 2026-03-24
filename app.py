# MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

import os
import sys
import signal
import subprocess
import time
import logging

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Imports
try:
    import psutil
except ImportError:
    logger.warning("psutil not available")
    psutil = None

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

try:
    from vnc_client import VNCClient
except ImportError as e:
    logger.error(f"Failed to import VNCClient: {e}")
    VNCClient = None

# Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024 * 1024  # 4GB
app.config['UPLOAD_FOLDER'] = '/app/uploads'

# Create directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('/app/disks', exist_ok=True)

# SocketIO
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=100 * 1024 * 1024
)


class EmulatorState:
    def __init__(self):
        self.process = None
        self.vnc_client = None
        self.running = False
        self.frame_greenlet = None
        self.config = {
            'ram': 512,
            'cores': 2,
            'cpu_model': 'pentium3',
            'machine': 'pc',
            'vga': 'std',
            'boot': 'cdrom',
            'iso': None,
            'disk_size': 'small',
            'performance': 'speed'
        }

    def reset(self):
        self.process = None
        self.vnc_client = None
        self.running = False
        self.frame_greenlet = None


emu_state = EmulatorState()

ALLOWED_EXTENSIONS = {'iso', 'img', 'qcow2', 'raw'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_system_stats():
    if psutil is None:
        return {
            'cpu_percent': 0, 'memory_used': 0, 'memory_total': 0,
            'memory_percent': 0, 'disk_used': 0, 'disk_total': 0, 'disk_percent': 0
        }
    
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage('/app')
        except:
            disk = psutil.disk_usage('/')
        
        return {
            'cpu_percent': round(cpu, 1),
            'memory_used': mem.used // (1024 * 1024),
            'memory_total': mem.total // (1024 * 1024),
            'memory_percent': round(mem.percent, 1),
            'disk_used': disk.used // (1024 * 1024 * 1024),
            'disk_total': disk.total // (1024 * 1024 * 1024),
            'disk_percent': round(disk.percent, 1)
        }
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return {
            'cpu_percent': 0, 'memory_used': 0, 'memory_total': 0,
            'memory_percent': 0, 'disk_used': 0, 'disk_total': 0, 'disk_percent': 0
        }


def get_available_isos():
    isos = []
    upload_dir = app.config['UPLOAD_FOLDER']
    
    if os.path.exists(upload_dir):
        for f in os.listdir(upload_dir):
            if allowed_file(f):
                path = os.path.join(upload_dir, f)
                try:
                    size = os.path.getsize(path) / (1024 * 1024)
                    isos.append({'name': f, 'size': round(size, 2)})
                except:
                    pass
    
    return isos


def ensure_disk_exists(disk_path, size_gb):
    if not os.path.exists(disk_path):
        try:
            subprocess.run(
                ['qemu-img', 'create', '-f', 'qcow2', disk_path, f'{size_gb}G'],
                check=True, capture_output=True
            )
            logger.info(f"Created disk: {disk_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create disk: {e}")
            return False
    return True


def kill_qemu():
    """Kill any existing QEMU processes"""
    try:
        subprocess.run(['pkill', '-9', 'qemu-system'], capture_output=True)
        eventlet.sleep(0.5)
    except:
        pass
    
    try:
        if os.path.exists('/tmp/qemu-monitor.sock'):
            os.remove('/tmp/qemu-monitor.sock')
    except:
        pass


def build_qemu_command(config):
    """Build QEMU command"""
    disk_configs = {
        'small': ('/app/disks/disk_small.qcow2', 2),
        'medium': ('/app/disks/disk_medium.qcow2', 8),
        'large': ('/app/disks/disk_large.qcow2', 20)
    }
    
    disk_path, disk_size = disk_configs.get(config.get('disk_size', 'small'), disk_configs['small'])
    ensure_disk_exists(disk_path, disk_size)
    
    cmd = [
        'qemu-system-i386',
        '-m', str(config.get('ram', 512)),
        '-cpu', config.get('cpu_model', 'pentium3'),
        '-machine', config.get('machine', 'pc'),
        '-vga', config.get('vga', 'std'),
        '-display', 'vnc=:0',
        '-usb',
        '-device', 'usb-tablet',
        '-rtc', 'base=localtime',
        '-monitor', 'unix:/tmp/qemu-monitor.sock,server,nowait',
        '-daemonize'
    ]
    
    # Performance
    if config.get('performance', 'speed') == 'speed':
        cmd.extend(['-smp', f"cores={config.get('cores', 2)},threads=1"])
        cmd.extend(['-accel', 'tcg,thread=multi'])
    else:
        cmd.extend(['-smp', '1'])
        cmd.extend(['-accel', 'tcg'])
    
    # Disk
    cmd.extend(['-drive', f'file={disk_path},format=qcow2,if=ide'])
    
    # ISO
    iso_name = config.get('iso')
    if iso_name:
        iso_path = os.path.join(app.config['UPLOAD_FOLDER'], iso_name)
        if os.path.exists(iso_path):
            cmd.extend(['-cdrom', iso_path])
            logger.info(f"Using ISO: {iso_path}")
    
    # Boot order
    if config.get('boot') == 'cdrom':
        cmd.extend(['-boot', 'd'])
    else:
        cmd.extend(['-boot', 'c'])
    
    # Network
    cmd.extend(['-net', 'nic,model=rtl8139', '-net', 'user'])
    
    return cmd


def frame_capture_loop():
    """Capture and send frames"""
    logger.info("Frame capture started")
    
    frame_interval = 1.0 / 20  # 20 FPS
    
    while emu_state.running and emu_state.vnc_client and emu_state.vnc_client.connected:
        try:
            start = time.time()
            
            frame = emu_state.vnc_client.get_frame()
            if frame:
                socketio.emit('frame', frame)
            
            elapsed = time.time() - start
            sleep_time = max(0.01, frame_interval - elapsed)
            eventlet.sleep(sleep_time)
            
        except Exception as e:
            logger.error(f"Frame capture error: {e}")
            eventlet.sleep(0.1)
    
    logger.info("Frame capture ended")


def start_emulator(config):
    """Start QEMU"""
    global emu_state
    
    if emu_state.running:
        stop_emulator()
        eventlet.sleep(1)
    
    emu_state.config = config
    kill_qemu()
    
    cmd = build_qemu_command(config)
    logger.info(f"Starting QEMU: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            logger.error(f"QEMU failed: {result.stderr}")
            return False
        
        # Wait for QEMU to start
        eventlet.sleep(3)
        
        # Check if running
        check = subprocess.run(['pgrep', '-f', 'qemu-system'], capture_output=True)
        if check.returncode != 0:
            logger.error("QEMU not running after start")
            return False
        
        emu_state.running = True
        
        # Connect VNC
        if VNCClient:
            logger.info("Connecting to VNC...")
            emu_state.vnc_client = VNCClient('127.0.0.1', 5900)
            
            if emu_state.vnc_client.connect():
                logger.info("VNC connected!")
                emu_state.frame_greenlet = eventlet.spawn(frame_capture_loop)
            else:
                logger.error("VNC connection failed")
                emu_state.vnc_client = None
        else:
            logger.error("VNCClient not available")
        
        return True
        
    except Exception as e:
        logger.error(f"Start error: {e}", exc_info=True)
        emu_state.running = False
        return False


def stop_emulator():
    """Stop QEMU"""
    global emu_state
    
    logger.info("Stopping emulator...")
    emu_state.running = False
    
    if emu_state.frame_greenlet:
        try:
            emu_state.frame_greenlet.kill()
        except:
            pass
    
    if emu_state.vnc_client:
        try:
            emu_state.vnc_client.disconnect()
        except:
            pass
    
    kill_qemu()
    emu_state.reset()
    logger.info("Emulator stopped")


# Routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'running': emu_state.running})


@app.route('/api/isos')
def list_isos():
    return jsonify(get_available_isos())


@app.route('/api/stats')
def stats():
    return jsonify(get_system_stats())


@app.route('/api/status')
def status():
    return jsonify({'running': emu_state.running, 'config': emu_state.config})


@app.route('/api/upload', methods=['POST'])
def upload_iso():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No filename'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid type'}), 400
    
    try:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        size = os.path.getsize(filepath) / (1024 * 1024)
        
        logger.info(f"Uploaded: {filename} ({size:.2f} MB)")
        
        return jsonify({'success': True, 'filename': filename, 'size': round(size, 2)})
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete/<filename>', methods=['DELETE'])
def delete_iso(filename):
    try:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(filename))
        if os.path.exists(filepath):
            os.remove(filepath)
            return jsonify({'success': True})
        return jsonify({'error': 'Not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Socket events
@socketio.on('connect')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")
    emit('status', {
        'running': emu_state.running,
        'config': emu_state.config,
        'isos': get_available_isos()
    })


@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")


@socketio.on('start')
def handle_start(config):
    logger.info(f"Start requested: {config}")
    success = start_emulator(config)
    emit('started', {'success': success})


@socketio.on('stop')
def handle_stop():
    logger.info("Stop requested")
    stop_emulator()
    emit('stopped', {'success': True})


@socketio.on('key')
def handle_key(data):
    """Handle keyboard input"""
    if not emu_state.running or not emu_state.vnc_client:
        return
    
    try:
        keysym = int(data.get('keysym', 0))
        down = bool(data.get('down', False))
        
        if keysym > 0:
            emu_state.vnc_client.send_key(keysym, down)
            
    except Exception as e:
        logger.error(f"Key error: {e}")


@socketio.on('mouse')
def handle_mouse(data):
    """Handle mouse input"""
    if not emu_state.running:
        logger.debug("Mouse ignored: not running")
        return
    
    if not emu_state.vnc_client:
        logger.debug("Mouse ignored: no VNC client")
        return
    
    if not emu_state.vnc_client.connected:
        logger.debug("Mouse ignored: VNC not connected")
        return
    
    try:
        x = int(data.get('x', 0))
        y = int(data.get('y', 0))
        buttons = int(data.get('buttons', 0))
        
        logger.debug(f"Mouse: x={x}, y={y}, buttons={buttons}")
        
        emu_state.vnc_client.send_mouse(x, y, buttons)
        
    except Exception as e:
        logger.error(f"Mouse error: {e}")


@socketio.on('reset')
def handle_reset():
    """Reset VM"""
    try:
        subprocess.run(
            ['bash', '-c', 'echo "system_reset" | socat - UNIX-CONNECT:/tmp/qemu-monitor.sock'],
            capture_output=True, timeout=2
        )
        emit('reset_done', {'success': True})
    except Exception as e:
        logger.error(f"Reset error: {e}")
        emit('reset_done', {'success': False})


@socketio.on('ctrl_alt_del')
def handle_cad():
    """Send Ctrl+Alt+Del"""
    try:
        subprocess.run(
            ['bash', '-c', 'echo "sendkey ctrl-alt-delete" | socat - UNIX-CONNECT:/tmp/qemu-monitor.sock'],
            capture_output=True, timeout=2
        )
    except Exception as e:
        logger.error(f"CAD error: {e}")


# Error handlers
@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large (max 4GB)'}), 413


@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Server error'}), 500


def cleanup(signum=None, frame=None):
    logger.info("Cleanup...")
    stop_emulator()
    sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)


if __name__ == '__main__':
    # Create disks
    for name, size in [('small', 2), ('medium', 8), ('large', 20)]:
        ensure_disk_exists(f'/app/disks/disk_{name}.qcow2', size)
    
    logger.info("Starting server...")
    logger.info(f"ISOs: {get_available_isos()}")
    
    socketio.run(app, host='0.0.0.0', port=7860, debug=False, use_reloader=False) 
