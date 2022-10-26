import os
import time
import tempfile
import json
from pathlib import Path

import docker
import pandas as pd
import requests
import pytest

from .conftest import docker_inet_ip, TEMP_DIR


OVERRIDE_CONFIG = {
    'integrations': {},
    'api': {
        "http": {"host": docker_inet_ip()},
        "mysql": {"host": docker_inet_ip()}
    }
}
API_LIST = ["http", "mysql"]

HTTP_API_ROOT = f'http://{docker_inet_ip()}:47334/api'


class Dlist(list):
    """Service class for convenient work with list of dicts(db response)"""

    def __contains__(self, item):
        if item in self.__getitem__(0):
            return True
        return False

    def get_record(self, key, value):
        if key in self:
            for x in self:
                if x[key] == value:
                    return x
        return None


class BaseStuff:
    predictor_name = 'home_rentals'
    file_datasource_name = "from_files"

    @staticmethod
    def to_dicts(response):
        if not response:
            return {}
        lines = response.splitlines()
        if len(lines) < 2:
            return {}
        headers = tuple(lines[0].split("\t"))
        res = Dlist()
        for body in lines[1:]:
            data = tuple(body.split("\t"))
            res.append(dict(zip(headers, data)))
        return res

    def query(self, _query, encoding='utf-8'):
        """Run mysql docker container
           Perform connection to mindsdb database
           Execute sql request
           ----------------------
           It is very problematic (or even impossible)
           to provide sql statement as it is in 'docker run command',
           that's why this action is splitted on three steps:
               Save sql statement into temporary dir in .sql file
               Run docker container with volume points to this temp dir,
               Provide .sql file as input parameter for 'mysql' command"""
        print("ATTEMPT TO EXECUTE A QUERY: %s" % _query)
        with tempfile.TemporaryDirectory() as tmpdirname:
            with open(f"{tmpdirname}/test.sql", 'w') as f:
                f.write(_query)
            cmd = f"{self.launch_query_tmpl} < /temp/test.sql"
            cmd = 'sh -c "' + cmd + '"'
            res = self.docker_client.containers.run(
                self.mysql_image,
                command=cmd,
                remove=True,
                volumes={str(tmpdirname): {'bind': '/temp', 'mode': 'ro'}},
                environment={"MYSQL_PWD": self.config["api"]["mysql"]["password"]})
        return self.to_dicts(res.decode(encoding))

    def create_datasource(self, db_data):
        db_type = db_data["type"]
        _query = "CREATE DATASOURCE %s WITH ENGINE = '%s', PARAMETERS = %s;" % (
            db_type.upper(),
            db_type,
            json.dumps(db_data["connection_data"]))

        self.query(_query)
        # and try to drop one of the datasources
        if db_type == 'mysql':

            self.query(f'drop datasource {db_type};')
            # and create again
            self.query(_query)

    @staticmethod
    def upload_ds(df, name):
        """Upload pandas df as csv file."""
        with tempfile.NamedTemporaryFile(mode='w+', newline='', delete=False) as f:
            df.to_csv(f, index=False)
            f.flush()
            url = f'{HTTP_API_ROOT}/files/{name}'
            data = {
                "file": (f.name, f, 'text/csv')
            }
            res = requests.put(url, files=data)
            res.raise_for_status()

    def verify_file_ds(self, ds_name):
        timeout = 5
        threshold = time.time() + timeout
        res = ''
        while time.time() < threshold:
            _query = "USE files; SHOW tables;"
            res = self.query(_query)
            if 'Tables_in_files' in res and res.get_record('Tables_in_files', ds_name):
                break
            time.sleep(0.5)
        assert 'Tables_in_files' in res and res.get_record('Tables_in_files', ds_name), f"file datasource {ds_name} is not ready to use after {timeout} seconds"

    def check_predictor_readiness(self, predictor_name):
        timeout = 600
        threshold = time.time() + timeout
        res = ''
        while time.time() < threshold:
            _query = "SELECT status, error FROM predictors WHERE name='{}';".format(predictor_name)
            res = self.query(_query)
            if 'status' in res:
                if res.get_record('status', 'complete'):
                    break
                elif res.get_record('status', 'error'):
                    raise Exception(res[0]['error'])
            time.sleep(2)
        assert 'status' in res and res.get_record('status', 'complete'), f"predictor {predictor_name} is not complete after {timeout} seconds"

    def validate_datasource_creation(self, db_data):
        ds_type = db_data["type"]
        # self.create_datasource(ds_type.lower())
        res = self.query("SELECT * FROM mindsdb.datasources WHERE name='{}';".format(ds_type.upper()))
        assert "name" in res and res.get_record("name", ds_type.upper()), f"Expected datasource is not found after creation - {ds_type.upper()}: {res}"




@pytest.mark.usefixtures("mindsdb_app")
class TestMySqlApi(BaseStuff):

    @classmethod
    def setup_class(cls):

        cls.docker_client = docker.from_env()
        cls.mysql_image = 'mysql'
        cls.config = json.loads(Path(os.path.join(TEMP_DIR, "config.json")).read_text())

        cls.launch_query_tmpl = "mysql --host=%s --port=%s --user=%s --database=mindsdb" % (
            cls.config["api"]["mysql"]["host"],
            cls.config["api"]["mysql"]["port"],
            cls.config["api"]["mysql"]["user"])

    @classmethod
    def tear_down(cls):
        cls.docker_client.close()

    def test_create_postgres_datasources(self, postgres_db):
        self.create_datasource(postgres_db)
        self.validate_datasource_creation(postgres_db)
        # self.validate_datasource_creation(postgres_db)

    # @pytest.mark.parametrize("db_type", ["postgres", "mysql", "mariad"])
    # def test_1_create_datasources(self, db_type):
    #     self.validate_datasource_creation(db_type)

    # def test_2_create_predictor(self):
    #     _query = f"CREATE PREDICTOR {self.predictor_name} from MYSQL (select * from test_data.home_rentals) as hr_ds predict rental_price;"
    #     self.query(_query)
    #     self.check_predictor_readiness(self.predictor_name)

    # def test_3_making_prediction(self):
    #     _query = f"""
    #         SELECT rental_price, rental_price_explain
    #         FROM {self.predictor_name}
    #         WHERE number_of_rooms = 2 and sqft = 400 and location = 'downtown' and days_on_market = 2 and initial_price= 2500;
    #     """
    #     res = self.query(_query)
    #     self.assertTrue('rental_price' in res and 'rental_price_explain' in res,
    #                     f"error getting prediction from {self.predictor_name} - {res}")

    # def test_4_describe_predictor_attrs(self):
    #     attrs = ["model", "features", "ensemble"]
    #     for attr in attrs:
    #         with self.subTest(msg=attr):
    #             print(f"\nExecuting {self._testMethodName} ({__name__}.{self.__class__.__name__}) [{attr}]")
    #             self.query(f"describe mindsdb.{self.predictor_name}.{attr};")

    # def test_5_service_requests(self):
    #     service_requests = [
    #         "show databases;",
    #         "show schemas;",
    #         "show tables;",
    #         "show tables from mindsdb;",
    #         "show full tables from mindsdb;",
    #         "show variables;",
    #         "show session status;",
    #         "show global variables;",
    #         "show engines;",
    #         "show warnings;",
    #         "show charset;",
    #         "show collation;",
    #         "show datasources;",
    #         "show predictors;",
    #         "show function status where db = 'mindsdb';",
    #         "show procedure status where db = 'mindsdb';",
    #         # "show table status like commands;",
    #     ]
    #     for req in service_requests:
    #         with self.subTest(msg=req):
    #             print(f"\nExecuting {self._testMethodName} ({__name__}.{self.__class__.__name__}) [{req}]")
    #             self.query(req)

    # def test_7_train_predictor_from_files(self):
    #     df = pd.DataFrame({
    #         'x1': [x for x in range(100, 210)] + [x for x in range(100, 210)],
    #         'x2': [x * 2 for x in range(100, 210)] + [x * 3 for x in range(100, 210)],
    #         'y': [x * 3 for x in range(100, 210)] + [x * 2 for x in range(100, 210)]
    #     })
    #     file_predictor_name = "predictor_from_file"
    #     self.upload_ds(df, self.file_datasource_name)
    #     self.verify_file_ds(self.file_datasource_name)

    #     _query = f"""
    #         CREATE PREDICTOR {file_predictor_name}
    #         from files (select * from {self.file_datasource_name})
    #         predict y;
    #     """
    #     self.query(_query)
    #     self.check_predictor_readiness(file_predictor_name)

    # def test_8_0_select_from_files(self):
    #     _query = f"select * from files.{self.file_datasource_name};"
    #     self.query(_query)

    # def test_9_ts_train_and_predict(self):
    #     train_df = pd.DataFrame({
    #         'group': ["A" for _ in range(100, 210)] + ["B" for _ in range(100, 210)],
    #         'order': [x for x in range(100, 210)] + [x for x in range(200, 310)],
    #         'x1': [x for x in range(100, 210)] + [x for x in range(100, 210)],
    #         'x2': [x * 2 for x in range(100, 210)] + [x * 3 for x in range(100, 210)],
    #         'y': [x * 3 for x in range(100, 210)] + [x * 2 for x in range(100, 210)]
    #     })

    #     test_df = pd.DataFrame({
    #         'group': ["A" for _ in range(210, 220)] + ["B" for _ in range(210, 220)],
    #         'order': [x for x in range(210, 220)] + [x for x in range(310, 320)],
    #         'x1': [x for x in range(210, 220)] + [x for x in range(210, 220)],
    #         'x2': [x * 2 for x in range(210, 220)] + [x * 3 for x in range(210, 220)],
    #         'y': [x * 3 for x in range(210, 220)] + [x * 2 for x in range(210, 220)]
    #     })

    #     train_ds_name = "train_ts_file_ds"
    #     test_ds_name = "test_ts_file_ds"
    #     for df, ds_name in [(train_df, train_ds_name), (test_df, test_ds_name)]:
    #         self.upload_ds(df, ds_name)
    #         self.verify_file_ds(ds_name)

    #     params = [
    #         ("with_group_by_hor1",
    #             f"CREATE PREDICTOR %s from files (select * from {train_ds_name}) PREDICT y ORDER BY order GROUP BY group WINDOW 10 HORIZON 1;",
    #             f"SELECT res.group, res.y as PREDICTED_RESULT FROM files.{test_ds_name} as source JOIN mindsdb.%s as res WHERE source.group= 'A' LIMIT 1;",
    #             1),
    #         ("no_group_by_hor1",
    #             f"CREATE PREDICTOR %s from files (select * from {train_ds_name}) PREDICT y ORDER BY order WINDOW 10 HORIZON 1;",
    #             f"SELECT res.group, res.y as PREDICTED_RESULT FROM files.{test_ds_name} as source JOIN mindsdb.%s as res LIMIT 1;",
    #             1),
    #         ("with_group_by_hor2",
    #             f"CREATE PREDICTOR %s from files (select * from {train_ds_name}) PREDICT y ORDER BY order GROUP BY group WINDOW 10 HORIZON 2;",
    #             f"SELECT res.group, res.y as PREDICTED_RESULT FROM files.{test_ds_name} as source JOIN mindsdb.%s as res WHERE source.group= 'A' LIMIT 2;",
    #             2),
    #         ("no_group_by_hor2",
    #             f"CREATE PREDICTOR %s from files (select * from {train_ds_name}) PREDICT y ORDER BY order WINDOW 10 HORIZON 2;",
    #             f"SELECT res.group, res.y as PREDICTED_RESULT FROM files.{test_ds_name} as source JOIN mindsdb.%s as res LIMIT 2;",
    #             2),
    #     ]
    #     for predictor_name, create_query, select_query, res_len in params:
    #         with self.subTest(msg=predictor_name):
    #             print(f"\nExecuting {self._testMethodName} ({__name__}.{self.__class__.__name__}) [{predictor_name}]")
    #             self.query(create_query % predictor_name)
    #             self.check_predictor_readiness(predictor_name)
    #             res = self.query(select_query % predictor_name)
    #             self.assertTrue(len(res) == res_len, f"prediction result {res} contains more that {res_len} records")
