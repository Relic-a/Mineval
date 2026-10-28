import json, os, socket, subprocess, tempfile, time, urllib.request
from pathlib import Path
binary=str(Path('dist/mineval').resolve())
with tempfile.TemporaryDirectory() as tmp:
    env=os.environ|{'XDG_CONFIG_HOME':tmp+'/config'}
    base=[binary,'--data',tmp+'/data']
    setup=subprocess.run(base+['setup'],cwd=tmp,env=env,capture_output=True,text=True)
    print('setup',setup.returncode, json.loads(setup.stdout)['ready'])
    doctor=subprocess.run([binary,'doctor'],cwd=tmp,env=env,capture_output=True,text=True)
    print('doctor',doctor.returncode,json.loads(doctor.stdout)['ready'])
    result=subprocess.run([binary,'task','list'],cwd=tmp,env=env,capture_output=True,text=True,check=True)
    print('task list',result.stdout.strip())
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    proc=subprocess.Popen([binary,'observe','--port',str(port)],cwd=tmp,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        url=proc.stdout.readline().strip().split(': ', 1)[1]
        for _ in range(100):
            try:
                content=urllib.request.urlopen(url,timeout=1).read()
                assert len(content)>1000
                print('dashboard bytes',len(content));break
            except OSError: time.sleep(.1)
        else: raise RuntimeError('dashboard failed to start')
    finally:
        proc.terminate();proc.wait(timeout=10)
