import json, os, shutil, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path
import app

# Isolated test storage.
root = Path(tempfile.mkdtemp(prefix='drift-test-'))
app.DOWNLOAD_ROOT = root
app.DB_PATH = root / 'test.sqlite3'
app.DOWNLOAD_ROOT.mkdir(exist_ok=True)
app.init_db()

class FakeYDL:
    def __init__(self, opts):
        self.opts = opts
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def extract_info(self, url, download=False):
        if not download:
            return {
                'title': 'Test Video', 'duration_string': '0:03', 'thumbnail': 'https://example.com/t.jpg',
                'formats': [
                    {'height': 2160, 'vcodec': 'avc1'}, {'height': 1440, 'vcodec': 'avc1'},
                    {'height': 1080, 'vcodec': 'avc1'}, {'height': 720, 'vcodec': 'avc1'}
                ]
            }
        outtmpl = self.opts['outtmpl']
        job_dir = Path(outtmpl).parent
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / 'Test_Video.mp4'
        hook = self.opts['progress_hooks'][0]
        total = 2 * 1024 * 1024
        written = 0
        for _ in range(4):
            written += total // 4
            hook({'status':'downloading','downloaded_bytes':written,'total_bytes':total,'speed':1024*1024,'eta':max(0,4-written//(total//4))})
            time.sleep(0.01)
        target.write_bytes(b'0' * total)
        hook({'status':'finished','downloaded_bytes':total})
        return {'title':'Test Video','duration_string':'0:03'}

app.yt_dlp = type('FakeModule', (), {'YoutubeDL': FakeYDL})
app.KNOWN_HOSTS.add('example.com')
_orig_getaddrinfo = app.socket.getaddrinfo
def fake_getaddrinfo(host, *args, **kwargs):
    if host == 'example.com': return [(2, 1, 6, '', ('93.184.216.34', 443))]
    return _orig_getaddrinfo(host, *args, **kwargs)
app.socket.getaddrinfo = fake_getaddrinfo
app.EXECUTOR = __import__('concurrent.futures', fromlist=['ThreadPoolExecutor']).ThreadPoolExecutor(max_workers=2)
server = __import__('http.server', fromlist=['ThreadingHTTPServer']).ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base=f'http://127.0.0.1:{port}'

def req(path, method='GET', data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(base+path, data=body, method=method, headers={'Content-Type':'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=5) as x:
            return x.status, json.loads(x.read()) if 'application/json' in x.headers.get('Content-Type','') else x.read(), x.headers
    except urllib.error.HTTPError as e:
        raw=e.read();
        try: raw=json.loads(raw)
        except: pass
        return e.code, raw, e.headers

assert req('/api/health')[0] == 200
status, data, _ = req('/api/analyze','POST',{'url':'https://example.com/video'})
assert status == 200 and data['formats'][0]['quality']=='Best available' and data['formats'][1]['quality']=='2160p'
status, data, _ = req('/api/analyze','POST',{'url':'http://127.0.0.1/video'})
assert status == 400
status, data, _ = req('/api/downloads','POST',{'url':'https://example.com/video','quality':'1080p'})
assert status == 202
job_id=data['job']['id']
for _ in range(100):
    status, j, _=req('/api/downloads/'+job_id)
    if j['job']['status']=='ready': break
    time.sleep(.02)
assert j['job']['status']=='ready', j
status, body, headers=req('/api/downloads/'+job_id+'/file')
assert status==200 and len(body)==2*1024*1024 and headers.get('Accept-Ranges')=='bytes'
status, body, headers=req('/api/downloads/'+job_id+'/file', headers={'Range':'bytes=10-19'})
assert status==206 and len(body)==10 and headers.get('Content-Range')=='bytes 10-19/2097152'
status, body, _=req('/api/downloads/'+job_id+'/cancel','POST')
assert status==200 and body['job']['status']=='ready'
# Expired/not-found behavior
assert req('/api/downloads/not-a-real-job')[0]==404

# Cancellation during an active transfer must end in cancelled and clean temp files.
class SlowYDL(FakeYDL):
    def extract_info(self, url, download=False):
        if not download: return super().extract_info(url, download=False)
        outtmpl=self.opts['outtmpl']; job_dir=Path(outtmpl).parent; job_dir.mkdir(parents=True,exist_ok=True)
        hook=self.opts['progress_hooks'][0]; total=20*1024*1024
        for written in range(1024*1024, total+1, 1024*1024):
            hook({'status':'downloading','downloaded_bytes':written,'total_bytes':total,'speed':1024*1024,'eta':20-written//(1024*1024)})
            time.sleep(.03)
        (job_dir/'Slow.mp4').write_bytes(b'1'*total)
        hook({'status':'finished','downloaded_bytes':total})
        return {'title':'Slow','duration_string':'0:20'}
app.yt_dlp = type('FakeModule', (), {'YoutubeDL': SlowYDL})
status,data,_=req('/api/downloads','POST',{'url':'https://example.com/slow','quality':'best'})
assert status==202
slow_id=data['job']['id']
time.sleep(.05)
status,data,_=req('/api/downloads/'+slow_id+'/cancel','POST')
assert status==200 and data['job']['status']=='cancelled'
for _ in range(50):
    _,j,_=req('/api/downloads/'+slow_id)
    if j['job']['status']=='cancelled': break
    time.sleep(.02)
assert j['job']['status']=='cancelled'

# Oversized progress is rejected before a large output can be accepted.
class HugeYDL(FakeYDL):
    def extract_info(self,url,download=False):
        if not download:return super().extract_info(url,download=False)
        job_dir=Path(self.opts['outtmpl']).parent; job_dir.mkdir(parents=True,exist_ok=True)
        hook=self.opts['progress_hooks'][0]
        try: hook({'status':'downloading','downloaded_bytes':app.MAX_DOWNLOAD_BYTES+1,'total_bytes':app.MAX_DOWNLOAD_BYTES+1})
        except Exception as exc:
            raise exc
        return {'title':'Huge'}
app.yt_dlp=type('FakeModule',(),{'YoutubeDL':HugeYDL})
status,data,_=req('/api/downloads','POST',{'url':'https://example.com/huge','quality':'720p'})
assert status==202
huge_id=data['job']['id']
for _ in range(50):
    _,j,_=req('/api/downloads/'+huge_id)
    if j['job']['status'] in {'failed','cancelled','ready'}: break
    time.sleep(.02)
assert j['job']['status']=='failed' and '500 MB' in j['job']['error']
status, body, headers = req('/')
# req() parses non-JSON response as bytes
assert status == 200 and b'<title>' in body and headers.get('Content-Type','').startswith('text/html')
status, body, headers = req('/js/app.js')
assert status == 200 and b'ACTIVE_JOBS_KEY' in body

server.shutdown(); app.EXECUTOR.shutdown(wait=True, cancel_futures=True)
shutil.rmtree(root, ignore_errors=True)
print('ALL BACKEND INTEGRATION TESTS PASSED')
