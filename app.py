import os
import json
import time
import subprocess
import threading
import uuid
import psutil
import hashlib
import urllib.request
from flask import Flask, request, jsonify, send_from_directory, Response

app = Flask(__name__, static_folder='ui', static_url_path='')

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
FAVORITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favorites.json')
LOCKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locks.json')

# Store running/completed processes for resource monitoring
processes = {}


def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, 'r') as f:
            return json.load(f)
    return []


def save_favorites(favs):
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(favs, f)


def load_locks():
    if os.path.exists(LOCKS_FILE):
        with open(LOCKS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_locks(locks):
    with open(LOCKS_FILE, 'w') as f:
        json.dump(locks, f)


def check_lock(rel_path, provided_pass):
    locks = load_locks()
    if rel_path in locks:
        if not provided_pass:
            return False
        if hashlib.sha256(provided_pass.encode()).hexdigest() != locks[rel_path]:
            return False
    return True


def parse_script_metadata(filepath):
    """Parse metadata from script comment headers."""
    metadata = {
        'name': os.path.basename(filepath).replace('.sh', '').replace('_', ' ').title(),
        'desc': '',
        'tag': '',
        'path': filepath
    }
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('# name:'):
                    metadata['name'] = line[7:].strip()
                elif line.startswith('# desc:'):
                    metadata['desc'] = line[7:].strip()
                elif line.startswith('# tag:'):
                    metadata['tag'] = line[6:].strip()
                elif not line.startswith('#') and line:
                    break
    except Exception:
        pass
    return metadata


def get_all_scripts():
    """Walk scripts directory and return all scripts grouped by category."""
    categories = {}
    favorites = load_favorites()
    locks = load_locks()

    if not os.path.exists(SCRIPTS_DIR):
        os.makedirs(SCRIPTS_DIR)
        return categories

    for category in sorted(os.listdir(SCRIPTS_DIR)):
        cat_path = os.path.join(SCRIPTS_DIR, category)
        if os.path.isdir(cat_path):
            scripts = []
            for script_file in sorted(os.listdir(cat_path)):
                if script_file.endswith('.sh'):
                    full_path = os.path.join(cat_path, script_file)
                    rel_path = f"{category}/{script_file}"
                    meta = parse_script_metadata(full_path)
                    meta['file'] = script_file
                    meta['category'] = category
                    meta['relative_path'] = rel_path
                    meta['favorite'] = rel_path in favorites
                    meta['locked'] = rel_path in locks
                    scripts.append(meta)
            if scripts:
                categories[category] = scripts

    return categories


# ─── Routes ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('ui', 'index.html')


@app.route('/api/scripts')
def list_scripts():
    return jsonify(get_all_scripts())


@app.route('/api/scripts/content', methods=['POST'])
def get_script_content():
    data = request.json or {}
    rel_path = data.get('path', '')
    password = data.get('password', '')
    
    if not check_lock(rel_path, password):
        return jsonify({'error': 'Locked', 'locked': True}), 401
        
    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    # Security check
    if not full_path.startswith(os.path.normpath(SCRIPTS_DIR)):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.exists(full_path):
        return jsonify({'error': 'Script not found'}), 404

    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    return jsonify({'content': content, 'path': rel_path})


def _track_metrics(proc, result):
    cpu_percent = 0.0
    max_mem_mb = 0.0
    samples = 0
    total_cpu = 0.0
    try:
        p = psutil.Process(proc.pid)
        while proc.poll() is None:
            c = p.cpu_percent(interval=0.1)
            # rss is resident set size (memory)
            m = p.memory_info().rss / (1024 * 1024)
            total_cpu += c
            max_mem_mb = max(max_mem_mb, m)
            samples += 1
    except (psutil.NoSuchProcess, Exception):
        pass
    
    result['cpu'] = round(total_cpu / samples, 1) if samples > 0 else 0.0
    result['mem'] = round(max_mem_mb, 1)


@app.route('/api/scripts/run', methods=['POST'])
def run_script():
    data = request.json
    rel_path = data.get('path', '')
    password = data.get('password', '')
    
    if not check_lock(rel_path, password):
        return jsonify({'error': 'Locked', 'success': False}), 401
        
    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    # Security check
    if not full_path.startswith(os.path.normpath(SCRIPTS_DIR)):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.exists(full_path):
        return jsonify({'error': 'Script not found'}), 404

    run_id = str(uuid.uuid4())[:8]
    shell_cmd = _find_shell()

    def generate():
        try:
            start_time = time.time()
            proc = subprocess.Popen(
                [shell_cmd, full_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SCRIPTS_DIR,
                bufsize=1,
                universal_newlines=True
            )

            metrics = {'cpu': 0.0, 'mem': 0.0}
            t = threading.Thread(target=_track_metrics, args=(proc, metrics))
            t.start()
            
            yield f"data: {json.dumps({'type': 'system', 'content': f'Starting script execution... (ID: {run_id})\\n'})}\n\n"

            for line in iter(proc.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'type': 'stdout', 'content': line})}\n\n"

            proc.stdout.close()
            proc.wait(timeout=60)
            t.join(timeout=1)
            
            end_time = time.time()
            elapsed = end_time - start_time
            system_mem = psutil.virtual_memory().total / (1024 * 1024)
            mem_percent = (metrics['mem'] / system_mem * 100) if system_mem > 0 else 0

            resource_info = {
                'execution_time': round(elapsed, 3),
                'execution_time_formatted': _format_time(elapsed),
                'exit_code': proc.returncode,
                'cpu_percent': metrics['cpu'],
                'memory_used_mb': metrics['mem'],
                'memory_total_mb': round(system_mem, 1),
                'memory_percent': round(mem_percent, 2),
            }

            yield f"data: {json.dumps({'type': 'metrics', 'resources': resource_info, 'exit_code': proc.returncode, 'success': proc.returncode == 0})}\n\n"
        except subprocess.TimeoutExpired:
            proc.kill()
            yield f"data: {json.dumps({'type': 'error', 'content': '⏱️ Script timed out after 60 seconds'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'❌ Error: {str(e)}'})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/exec', methods=['POST'])
def exec_command():
    data = request.json
    command = data.get('command', '')

    if not command:
        return jsonify({'error': 'No command provided'}), 400

    shell_cmd = _find_shell()

    def generate():
        try:
            # Need to format for Windows/Linux subshells correctly
            args = [shell_cmd, '-c', command] if shell_cmd != 'cmd' else ['cmd', '/c', command]
            
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SCRIPTS_DIR,
                bufsize=1,
                universal_newlines=True
            )
            
            for line in iter(proc.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'type': 'stdout', 'content': line})}\n\n"
                    
            proc.stdout.close()
            proc.wait(timeout=60)
            yield f"data: {json.dumps({'type': 'metrics', 'exit_code': proc.returncode, 'success': proc.returncode == 0})}\n\n"
        except subprocess.TimeoutExpired:
            proc.kill()
            yield f"data: {json.dumps({'type': 'error', 'content': '⏱️ Command timed out after 60 seconds'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'❌ Error: {str(e)}'})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/scripts/save', methods=['POST'])
def save_script():
    data = request.json
    category = data.get('category', '').strip()
    filename = data.get('filename', '').strip()
    content = data.get('content', '')
    provided_pass = data.get('password', '')

    if not category or not filename:
        return jsonify({'error': 'Category and filename required'}), 400

    if not filename.endswith('.sh'):
        filename += '.sh'

    category = category.replace('..', '').replace('/', '').replace('\\', '')
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    rel_path = f'{category}/{filename}'
    
    if not check_lock(rel_path, provided_pass):
        return jsonify({'error': 'Locked', 'success': False}), 401

    cat_dir = os.path.join(SCRIPTS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)

    full_path = os.path.join(cat_dir, filename)
    with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

    return jsonify({'success': True, 'path': rel_path})


@app.route('/api/scripts/delete', methods=['DELETE'])
def delete_script():
    data = request.json or {}
    rel_path = request.args.get('path', '') or data.get('path', '')
    provided_pass = data.get('password', '')
    
    if not check_lock(rel_path, provided_pass):
        return jsonify({'error': 'Locked', 'success': False}), 401
        
    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    if not full_path.startswith(os.path.normpath(SCRIPTS_DIR)):
        return jsonify({'error': 'Invalid path'}), 403

    if os.path.exists(full_path):
        os.remove(full_path)
        # Clean up favs
        favs = load_favorites()
        if rel_path in favs:
            favs.remove(rel_path)
            save_favorites(favs)
        # Clean up locks
        locks = load_locks()
        if rel_path in locks:
            del locks[rel_path]
            save_locks(locks)
        return jsonify({'success': True})

    return jsonify({'error': 'Script not found'}), 404


@app.route('/api/scripts/favorite', methods=['POST'])
def toggle_favorite():
    data = request.json
    rel_path = data.get('path', '')
    favs = load_favorites()

    if rel_path in favs:
        favs.remove(rel_path)
        is_fav = False
    else:
        favs.append(rel_path)
        is_fav = True

    save_favorites(favs)
    return jsonify({'favorite': is_fav})


@app.route('/api/scripts/lock', methods=['POST'])
def manage_lock():
    data = request.json
    rel_path = data.get('path', '')
    old_pass = data.get('old_password', '')
    new_pass = data.get('new_password', '') # empty string removes lock!
    
    # Verify current lock
    if not check_lock(rel_path, old_pass):
        return jsonify({'error': 'Incorrect current password', 'success': False}), 401
        
    locks = load_locks()
    if new_pass:
        locks[rel_path] = hashlib.sha256(new_pass.encode()).hexdigest()
    else:
        if rel_path in locks:
            del locks[rel_path]

    save_locks(locks)
    return jsonify({'success': True, 'locked': bool(new_pass)})


@app.route('/api/scripts/import_github', methods=['POST'])
def import_github():
    data = request.json
    url = data.get('url', '').strip()
    category = data.get('category', '').strip()
    filename = data.get('filename', '').strip()

    if not url or not category or not filename:
        return jsonify({'error': 'Missing fields', 'success': False}), 400

    if not filename.endswith('.sh'):
        filename += '.sh'

    # Convert standard github url to raw
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 DevShell'})
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8')
    except Exception as e:
        return jsonify({'error': f'Failed to fetch from GitHub: {str(e)}', 'success': False}), 400

    category = category.replace('..', '').replace('/', '').replace('\\', '')
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    
    # We will overwrite without checking pass since it doesn't exist yet, but if it does we should check pass.
    rel_path = f'{category}/{filename}'
    if not check_lock(rel_path, ''):
        return jsonify({'error': 'File exists and is locked!', 'success': False}), 401

    cat_dir = os.path.join(SCRIPTS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    full_path = os.path.join(cat_dir, filename)
    with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

    return jsonify({'success': True, 'path': rel_path})


# ─── Helpers ──────────────────────────────────────────────────────

def _find_shell():
    """Find available bash shell on the system."""
    candidates = [
        r'C:\Program Files\Git\bin\bash.exe',
        r'C:\Program Files (x86)\Git\bin\bash.exe',
        'bash',
        'sh',
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return 'bash'


def _format_time(seconds):
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}µs"
    elif seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    elif seconds < 60:
        return f"{seconds:.3f}s"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.1f}s"


# ─── Main ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("[*] DevShell starting on http://localhost:5000")
    print(f"[*] Scripts directory: {SCRIPTS_DIR}")
    app.run(debug=True, port=5000)
