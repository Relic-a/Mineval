"""Fixed black-box HTTP checks; private assertions stay in the trusted process.

The disposable candidate sandbox runs the service and a small request transport.
The transport sees each request, never the remaining test plan or expected values.
"""
import base64
import hashlib
import json
import time
from pathlib import Path
from mineval.core.artifacts import copy_tree, relative, write_json
from mineval.core.sandbox import Limits, SandboxSpec

# No shell, redirects or remote targets. stdout is a transport frame, not a verdict.
TRANSPORT = '''import base64,http.client,json,sys
r=json.loads(base64.b64decode(sys.argv[1])); c=http.client.HTTPConnection('127.0.0.1',int(sys.argv[2]),timeout=5)
c.request(r['method'],r['path'],body=base64.b64decode(r['body']),headers=r['headers'])
s=c.getresponse(); b=s.read(1048577)
print(json.dumps({'status':s.status,'headers':dict(s.getheaders()),'body':base64.b64encode(b).decode()}))
'''
START = '''import base64,json,os,subprocess,sys
argv=json.loads(base64.b64decode(sys.argv[1])); subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
'''


def _document(value):
    if not isinstance(value, dict) or set(value) != {'cases'} or not isinstance(value['cases'], list) or not value['cases']:
        raise ValueError('HTTP tests require a nonempty case list')
    for case in value['cases']:
        if (not isinstance(case, dict) or set(case) != {'name', 'steps'} or not isinstance(case['name'], str)
            or not case['name'].strip() or not isinstance(case['steps'], list) or not case['steps']):
            raise ValueError('Each HTTP case needs a name and steps')
        for step in case['steps']:
            if (not isinstance(step, dict) or set(step) - {'method', 'path', 'headers', 'body', 'expect'} or
                not {'method', 'path', 'expect'} <= set(step) or step['method'] not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS') or
                not isinstance(step['path'], str) or not step['path'].startswith('/') or step['path'].startswith('//') or
                '\r' in step['path'] or '\n' in step['path'] or len(step['path']) > 8192):
                raise ValueError('Invalid HTTP request')
            headers = step.get('headers', {})
            if (not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) or '\n' in k+v or '\r' in k+v for k,v in headers.items()) or
                not isinstance(step.get('body', ''), str) or len(step.get('body', '').encode()) > 65536):
                raise ValueError('Invalid HTTP request headers or body')
            expect = step['expect']
            if (not isinstance(expect, dict) or set(expect) - {'status', 'body', 'json', 'headers'} or
                'status' not in expect or type(expect['status']) is not int or not 100 <= expect['status'] <= 599 or
                ('body' in expect and not isinstance(expect['body'], str)) or
                ('headers' in expect and (not isinstance(expect['headers'], dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in expect['headers'].items())))):
                raise ValueError('Every HTTP step needs a valid expected status')
    return value['cases']


def execute(source, destination, staged, verification, artifacts, config, image, runner):
    staged.mkdir(); report = {'verdict': 'error', 'explanation': 'HTTP verification failed.', 'commands': [], 'cases': []}
    try:
        cases, selected, total_bytes = [], [], 0
        for item in artifacts:
            raw = (verification / relative(item['stored_path'])).read_bytes()
            total_bytes += len(raw)
            if len(raw) > 1048576 or total_bytes > 10485760: raise ValueError('HTTP test data too large')
            cases.extend(_document(json.loads(raw)))
            selected.append(item | {'executed_sha256': hashlib.sha256(raw).hexdigest(), 'adapted': False})
        if not cases or len(cases) > 1000 or sum(len(c['steps']) for c in cases) > 5000 or len({c['name'] for c in cases}) != len(cases):
            raise ValueError('HTTP cases must be unique and within limits')
        write_json(staged / 'selected-checks.json', selected)
        copy_tree(source, destination)
        limits = Limits(**config.get('limits', {}))
        spec = SandboxSpec(image, ('/bin/sh', '-c', 'while :; do sleep 1; done'), limits,
                           workspace=destination, network='none', workdir=config.get('workdir', '/workspace'), services=config.get('services', ()))
        with runner.container(spec) as container:
            container.start()
            report.update(image_id=container.image_id, container_id=container.id)
            service_images = getattr(container, 'service_images', None)
            if isinstance(service_images, dict):
                report['service_images'] = service_images
            deadline = time.monotonic() + limits.timeout_seconds
            def run(argv):
                budget = deadline - time.monotonic()
                if budget <= 0: raise TimeoutError('HTTP verification deadline exhausted')
                result = container.exec(argv, timeout=min(budget, 10), workdir=spec.workdir)
                if result['exit_code'] != 0 or result['timed_out'] or result['oom_killed'] or result['logs_truncated']:
                    raise RuntimeError('HTTP transport or setup failed')
                return result
            for command in config['commands']:
                if command['kind'] != 'tests':
                    result = run(command['argv']); report['commands'].append({'name': command['name'], 'kind': command['kind'], 'status': 'passed', **result})
            encoded = base64.b64encode(json.dumps(config['service']['argv']).encode()).decode()
            run(['python3', '-c', START, encoded])
            # The transport cannot access the private document; only the current request crosses.
            for case in cases:
                outcome = {'name': case['name'], 'status': 'passed', 'steps': []}
                report['cases'].append(outcome)
                for step in case['steps']:
                    request = {'method': step['method'], 'path': step['path'], 'headers': step.get('headers', {}),
                               'body': base64.b64encode(step.get('body', '').encode()).decode()}
                    payload = base64.b64encode(json.dumps(request).encode()).decode()
                    while True:
                        try:
                            result = run(['python3', '-c', TRANSPORT, payload, str(config['service']['port'])]); break
                        except RuntimeError:
                            if report['cases'][0] is not outcome or outcome['steps'] or deadline - time.monotonic() < 1: raise
                            time.sleep(.1)
                    response = json.loads(result['stdout'])
                    body = base64.b64decode(response['body'], validate=True)
                    if len(body) > 1048576: raise ValueError('HTTP response too large')
                    expected = step['expect']; errors = []
                    if response['status'] != expected['status']: errors.append('status')
                    if 'body' in expected and body != expected['body'].encode(): errors.append('body')
                    if 'json' in expected:
                        try: actual_json = json.loads(body)
                        except (ValueError, UnicodeDecodeError): errors.append('invalid JSON')
                        else:
                            if actual_json != expected['json']: errors.append('json')
                    actual_headers = {k.lower(): v for k,v in response['headers'].items()}
                    if any(actual_headers.get(k.lower()) != v for k,v in expected.get('headers', {}).items()): errors.append('headers')
                    outcome['steps'].append({'status': 'failed' if errors else 'passed', 'mismatches': errors,
                                             'response_status': response['status'], 'response_body': body.decode('utf-8', 'replace')})
                    if errors: outcome['status'] = 'failed'
            if len(report['cases']) != len(cases) or any(len(r['steps']) != len(c['steps']) for r,c in zip(report['cases'],cases)):
                raise RuntimeError('Incomplete HTTP cases')
            failed = any(case['status'] != 'passed' for case in report['cases'])
            report.update(verdict='fail' if failed else 'pass', explanation='HTTP behavioral checks failed.' if failed else 'All HTTP behavioral checks passed.')
    except KeyboardInterrupt: raise
    except Exception as exc:
        report.update(verdict='error', error_kind=type(exc).__name__, explanation='HTTP verification could not complete.')
    write_json(staged / 'execution.json', report)
    return report
