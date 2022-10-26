import sys
import os
import time
import json
import subprocess
from pathlib import Path
import requests
import docker
import pytest
import netifaces

USE_PERSISTENT_STORAGE = bool(int(os.getenv('USE_PERSISTENT_STORAGE') or "0"))
TEST_CONFIG = os.path.dirname(os.path.realpath(__file__)) + '/config/config.json'
TEMP_DIR = Path(__file__).parent.absolute().joinpath('../../').joinpath(
    f'temp/test_storage_{int(time.time()*1000)}/' if not USE_PERSISTENT_STORAGE else 'temp/test_storage/'
).resolve()
TEMP_DIR.mkdir(parents=True, exist_ok=True)



def docker_inet_ip():
    if "docker0" not in netifaces.interfaces():
        raise Exception("Unable to find 'docker' interface. Please install docker first.")
    return netifaces.ifaddresses('docker0')[netifaces.AF_INET][0]['addr']

@pytest.fixture(scope="session")
def temp_dir():
    temp_dir = Path(__file__).parent.absolute().joinpath('../../').joinpath(
        f'temp/test_storage_{int(time.time()*1000)}/' if not USE_PERSISTENT_STORAGE else 'temp/test_storage/'
    ).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir

@pytest.fixture(scope="session")
def config(temp_dir):
    with open(TEST_CONFIG, 'rt') as f:
        config_json = json.loads(f.read())
        config_json['storage_dir'] = f'{TEMP_DIR}'
        config_json['storage_db'] = f'sqlite:///{TEMP_DIR}/mindsdb.sqlite3.db?check_same_thread=False&timeout=30'
        config_json['integrations'] = {}

    return config_json

def override_recursive(a, b):
    for key in b:
        if isinstance(b[key], dict) is False:
            a[key] = b[key]
        elif key not in a or isinstance(a[key], dict) is False:
            a[key] = b[key]
        # make config section empty by demand
        elif isinstance(b[key], dict) is True and b[key] == {}:
            a[key] = b[key]
        else:
            override_recursive(a[key], b[key])

@pytest.fixture(scope="module")
def mindsdb_app(request, config):
    apis = getattr(request.module, "API_LIST", [])
    if not apis:
        api_str = "http,mysql"
    else:
        api_str = ",".join(apis)
    to_override_conf = getattr(request.module, "OVERRIDE_CONFIG", {})
    if to_override_conf:
        override_recursive(config, to_override_conf)
    config_path = TEMP_DIR.joinpath('config.json')
    with open(config_path, "wt") as f:
        f.write(json.dumps(config))

    os.environ['CHECK_FOR_UPDATES'] = '0'
    print('Starting mindsdb process!')
    app = subprocess.Popen(
        ['python3', '-m', 'mindsdb', f'--api={api_str}', f'--config={config_path}', '--verbose'],
        close_fds=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
        shell=False
    )
    threshold = time.time() + 30

    while True:
        try:
            print("checking mindsdb app readiness.")
            host = config["api"]["http"]["host"]
            port = config["api"]["http"]["port"]
            r = requests.get(f"http://{host}:{port}/api/util/ping")
            r.raise_for_status()
            print("application is ready.")
            time.sleep(1)
            break
        except Exception:
            time.sleep(1)
            if time.time() > threshold:
                raise Exception("unable to launch mindsdb app in 30 seconds")
    def cleanup():
        print(f"STOPPING APPLICATION")
        app.kill()
        app.wait()
    request.addfinalizer(cleanup)
    return

@pytest.fixture(scope="function")
def postgres_db():
    image_name = "mindsdb/postgres-handler-test"
    docker_client = docker.from_env()
    container = None

    connection_args = {
                        "host": "172.17.0.1",
                        "port": "15432",
                        "user": "postgres",
                        "password": "supersecret",
                        "database": "test",
                      }

    def waitReadiness(container, timeout=30):
        threshold = time.time() + timeout
        ready_msg = "database system is ready to accept connections"
        while True:
            lines = container.logs().decode()
                # container fully ready
                # because it reloads the db server during initialization
                # need to check that the 'ready for connections' has found second time
            if lines.count(ready_msg) >= 2:
                break
            if time.time() > threshold:
                raise Exception("timeout exceeded, container is still not ready")
    try:
        container = docker_client.containers.run(
                    image_name,
                    detach=True,
                    environment={"POSTGRES_PASSWORD":"supersecret"},
                    ports={"5432/tcp": 15432},
                )
        waitReadiness(container)
    except Exception as e:
        if container is not None:
            container.kill()
        raise e

    yield {"type": "postgres",
           "connection_data": connection_args}

    container.kill()
    docker_client.close()
