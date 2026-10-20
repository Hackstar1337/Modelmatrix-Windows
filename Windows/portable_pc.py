"""Local portable Windows bundle entry point; no global install or PATH changes."""
from __future__ import annotations
import argparse, hashlib, json, os, socket, subprocess, sys, time, urllib.error, urllib.request, webbrowser
from pathlib import Path
from datetime import datetime, timezone


def read_json(path: Path) -> dict:
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict): raise ValueError(f'{path.name} must contain an object')
    return value


def request(base: str, path: str, body: dict | None=None, timeout: float=12) -> dict:
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(base+path,data=data,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        detail=error.read().decode('utf-8','replace')
        raise RuntimeError(f'HTTP {error.code}: {detail}') from error


def settings(root: Path) -> dict:
    path=root/'Data'/'portable-settings.json'
    result={'controller_port':18700,'relay_port':18701,'model_port':18710,'backend':'cpu'}
    if path.exists():result.update(read_json(path))
    for key in ('controller_port','relay_port','model_port'):
        if type(result[key]) is not int or not 1024<=result[key]<=65535: raise ValueError(f'{key} must be an integer port from 1024 to 65535')
    if len({result[k] for k in ('controller_port','relay_port','model_port')})!=3:raise ValueError('Controller, relay and model ports must differ')
    if result['model_port']==65535 or result['model_port']+1 in (result['controller_port'],result['relay_port']):raise ValueError('The optional GPU model port (model_port + 1) must also be free of configured port conflicts')
    if result['backend'] not in ('cpu','vulkan'):raise ValueError('backend must be cpu or vulkan')
    return result


def asset(root: Path, relative: str) -> Path:
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():raise ValueError(f'Missing/invalid bundled asset: {relative}')
    return path


def owned_controller(root: Path, info: dict | None=None) -> bool:
    """Never stop a PID without exact path and native creation-time agreement."""
    from platform_support import process_identity
    record_file=root/'Data'/'run'/'controller.pid'
    if not record_file.exists():return False
    try:
        record=read_json(record_file); identity=process_identity(int(record['pid']))
        if identity is None or identity.created_ns is None:return False
        expected=float(datetime.fromisoformat(record['started_utc']).timestamp())
        actual=identity.created_ns/1e9-11644473600
        same=lambda a,b:os.path.normcase(os.path.realpath(str(a)))==os.path.normcase(os.path.realpath(str(b)))
        return (abs(actual-expected)<0.002 and same(record['root'],root/'Data') and same(record['controller'],Path(__file__).with_name('controller.py')) and same(record['executable'],identity.executable) and (info is None or same(info['controller_root'],root/'Data')))
    except (OSError,ValueError,KeyError,TypeError):return False


def listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(.3);return sock.connect_ex(('127.0.0.1',port))==0


def configure_profiles(root: Path, manifest: dict, config: dict) -> None:
    profiles=root/'Data'/'profiles';profiles.mkdir(parents=True,exist_ok=True)
    model=asset(root,manifest['model']['path'])
    for backend,engine in manifest['engines'].items():
        server=asset(root,engine['path']);name='smollm2-'+backend
        path=profiles/(name+'.json')
        if path.exists() and read_json(path).get('PORTABLE_BUNDLE_PROFILE')!='smollm2-135m-v1':
            raise ValueError(f'{name} exists without the bundle marker; rename it rather than overwrite user data')
        argv=[str(server),'-m',str(model),'--host','127.0.0.1','--port','{port}','--alias','smollm2-135m','--ctx-size','2048','--threads','4','--n-gpu-layers','0' if backend=='cpu' else '99']
        values={'DISPLAY_NAME':'SmolLM2 135M Instruct ('+backend.upper()+')','REQUEST_MODEL':'smollm2-135m','SERVER_MODEL_ID':'smollm2-135m','RUNTIME':'llama.cpp','HOST':'127.0.0.1','PORT':str(config['model_port']+(0 if backend=='cpu' else 1)),'MODEL_FILE':str(model),'WORKING_DIRECTORY':str(server.parent),'START_COMMAND_JSON':json.dumps(argv),'CAPTURE_MODE':'1','PORTABLE_BUNDLE_PROFILE':'smollm2-135m-v1'}
        # Preserve user choices while updating only bundle-managed paths/ports on relocation.
        if path.exists():
            existing=read_json(path)
            for key in ('DISPLAY_NAME','CAPTURE_MODE'):values[key]=existing.get(key,values[key])
        path.write_text(json.dumps(values,indent=2),encoding='utf-8')


def start(root: Path, manifest: dict, config: dict, open_browser: bool) -> dict:
    base=f"http://127.0.0.1:{config['controller_port']}"
    if listening(config['controller_port']):
        info=request(base,'/api/dashboard/info')
        if not owned_controller(root,info):raise RuntimeError('Controller port belongs to another process; edit Data/portable-settings.json, do not kill that process')
        result={'ok':True,'already_running':True,'url':base}
    else:
        if listening(config['relay_port']):raise RuntimeError('Relay port is occupied; choose another relay_port in Data/portable-settings.json')
        configure_profiles(root,manifest,config)
        logs=root/'Data'/'Logs';logs.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,str(Path(__file__).with_name('controller.py')),'--root',str(root/'Data'),'--profiles-dir',str(root/'Data'/'profiles'),'--port',str(config['controller_port']),'--relay-port',str(config['relay_port']),'--no-browser']
        flags=(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS) if os.name=='nt' else 0
        environment=os.environ.copy()
        environment['PATH']=str(asset(root,manifest['engines']['cpu']['path']).parent)+os.pathsep+environment.get('PATH','')
        environment['MODELMATRIX_MODELS_DIR']=str(root/'Data'/'Models')
        with (logs/'controller.log').open('ab',buffering=0) as out,(logs/'controller-error.log').open('ab',buffering=0) as err:
            child=subprocess.Popen(command,cwd=str(root),stdin=subprocess.DEVNULL,stdout=out,stderr=err,close_fds=True,creationflags=flags,env=environment)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError('Controller exited during startup; inspect Data/Logs/controller-error.log')
            try:
                info=request(base,'/api/dashboard/info',timeout=2)
                if owned_controller(root,info):break
            except (OSError,RuntimeError):pass
            time.sleep(.25)
        else:
            # We own this Popen handle, so cancellation cannot hit a reused/unrelated PID.
            child.terminate();child.wait(timeout=8)
            raise RuntimeError('Controller readiness timed out; inspect Data/Logs')
        result={'ok':True,'already_running':False,'url':base,'pid':child.pid}
    if open_browser:webbrowser.open(base+'/')
    return result


def stop(root: Path, config: dict) -> dict:
    base=f"http://127.0.0.1:{config['controller_port']}"
    if not listening(config['controller_port']):return {'ok':True,'stopped':False,'reason':'not-listening'}
    info=request(base,'/api/dashboard/info')
    if not owned_controller(root,info):raise RuntimeError('Ownership mismatch; refusing to stop another controller')
    request(base,'/api/shutdown',{})
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if not listening(config['controller_port']) and not owned_controller(root):return {'ok':True,'stopped':True,'forced':False}
        time.sleep(.2)
    raise RuntimeError('Graceful shutdown timeout; inspect logs rather than force-kill an unverified PID')


def doctor(root: Path, manifest: dict, config: dict) -> dict:
    model=asset(root,manifest['model']['path'])
    with model.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=manifest['model']['sha256']:raise RuntimeError('Bundled model checksum mismatch')
    for engine in manifest['engines'].values():asset(root,engine['path'])
    result={'ok':True,'python':sys.version.split()[0],'model_checksum':'verified','backend':config['backend'],'ports':{k:v for k,v in config.items() if k.endswith('port')},'controller_running':listening(config['controller_port'])}
    if result['controller_running']:
        base=f"http://127.0.0.1:{config['controller_port']}";info=request(base,'/api/dashboard/info')
        result['owned_controller']=owned_controller(root,info)
        if not result['owned_controller']:raise RuntimeError('Controller port is occupied by an unrelated instance')
        result['status']=request(base,'/api/status')
    return result


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','doctor','chat','tray'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv);root=args.root.resolve()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'RemoteAgent'))
    try:
        manifest=read_json(root/'bundle.json');config=settings(root)
        if args.action=='stop':result=stop(root,config)
        elif args.action=='doctor':result=doctor(root,manifest,config)
        elif args.action=='start':result=start(root,manifest,config,not args.no_browser)
        elif args.action=='tray':
            start(root,manifest,config,False)
            os.environ['MODELMATRIX_DATA_ROOT']=str(root/'Data')
            os.environ['MODELMATRIX_CONTROLLER_URL']=f"http://127.0.0.1:{config['controller_port']}"
            import tray
            return tray.main(['--no-browser'])
        else:
            start(root,manifest,config,False)
            profile='smollm2-'+config['backend'];base=f"http://127.0.0.1:{config['controller_port']}"
            request(base,'/api/start',{'profile':profile},timeout=70)
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                status=request(base,'/api/status');row=next((r for r in status['statuses'] if r['profile']==profile),{})
                if row.get('ready'):break
                time.sleep(.3)
            else:raise RuntimeError('Small-model readiness timeout; inspect model logs')
            public=config['model_port']+(1 if config['backend']=='vulkan' else 0)
            result=request(f'http://127.0.0.1:{public}','/v1/chat/completions',{'model':'smollm2-135m','messages':[{'role':'user','content':'What is the capital of France? Answer briefly.'}],'temperature':0,'max_tokens':48},timeout=60)
        print(json.dumps(result,ensure_ascii=True,indent=2));return 0
    except (OSError,ValueError,RuntimeError,KeyError,subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=True),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
