import json
import logging
import math
import os
from typing import Any, Dict, List, Mapping

import pandas as pd
import yaml
from fastapi import APIRouter
from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import insert

router = APIRouter(tags=["Emporus Management"])


class EmporusSQLiteDatabase:
    def __init__(self, db_path: str):
        self.db_name = os.path.basename(db_path)
        self.db_path = db_path
        self.db_path = f'sqlite:///{os.path.join(db_path)}'
        self.engine = create_engine(self.db_path, connect_args={'check_same_thread': False})

    def get_trades(self, query: str, params: list[Any] | Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
        with self.engine.connect() as connection:
            print(f"Query: {query}, Params: {params}")
            trades_list = pd.read_sql_query(query, connection.connection, params=params).to_dict('records')
            print(f"Fetched {len(trades_list)} trades from {self.db_path}")
        # Convert stringified JSON fields to JSON
        for trade in trades_list:
            for key in ["entry_details", "exit_details", "raw_model_output"]:
                if key in trade and isinstance(trade[key], str):
                    trade[key] = json.loads(trade[key])
        return trades_list


class EmporusPostgresDatabase:
    def __init__(self):
        username = os.getenv("POSTGRES_USERNAME", "postgres")
        password = os.getenv("POSTGRES_PASSWORD", "postgres")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = int(os.getenv("POSTGRES_PORT", 5432))
        database = os.getenv("POSTGRES_DATABASE", "hummingbot")
        db_connection_string = f"postgresql+psycopg2://{username}:{password}@{host}:{port}/{database}"

        self.engine = create_engine(
            db_connection_string,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            connect_args={"connect_timeout": 10}
        )

    def get_trades(self, query: str, params: list[Any] | Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
        with self.engine.connect() as connection:
            trades_list = pd.read_sql_query(query, connection.connection, params=params).to_dict('records')
        return trades_list

    def save_trades(self, trades_list: List[Dict[str, Any]]):
        if not trades_list:
            return

        df = pd.DataFrame(trades_list)

        # Convert stringified JSON fields to JSON
        for field in ["entry_details", "exit_details", "raw_model_output"]:
            if field in df.columns:
                df[field] = df[field].apply(
                    lambda x: x if isinstance(x, (dict, list)) else json.loads(x) if isinstance(x, str) else None)

        # Replace NaN with None
        df = df.where(pd.notnull(df), None)
        try:
            metadata = MetaData()
            trades_table = Table("EmporusTrades", metadata, autoload_with=self.engine)

            with self.engine.begin() as conn:
                stmt = insert(trades_table).values(
                    df.to_dict(orient="records")).on_conflict_do_nothing()
                conn.execute(stmt)

            return
        except Exception as e:
            logging.error(f"Error saving trades: {str(e)}")


class EmporusTradeManager:
    def __init__(self):
        self.postgres_db = EmporusPostgresDatabase()
        self.sqlite_dbs: Dict[str, EmporusSQLiteDatabase] = {}

    def get_trades_from_sqlite(self, query: str, params: list[Any] | Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
        query = query.replace("%s", "?")
        for db_path in self.get_local_databases():
            if db_path not in self.sqlite_dbs:
                self.sqlite_dbs[db_path] = EmporusSQLiteDatabase(db_path)

        trades_list = []
        for db in self.sqlite_dbs.values():
            trades_list += db.get_trades(query, params)
        return trades_list

    def get_trades(self, start_time: int = None, end_time: int = None) -> Dict[str, Any]:
        request_query = "SELECT * FROM \"EmporusTrades\" WHERE 1=1"
        query_params = []

        if start_time:
            request_query += " AND \"update_timestamp\" > %s"
            query_params.append(start_time)

        if end_time:
            request_query += " AND \"update_timestamp\" < %s"
            query_params.append(end_time)

        try:
            trades_sql = self.get_trades_from_sqlite(request_query, query_params)
            # trades_pgs = self.postgres_db.get_trades(request_query, query_params)
        except Exception as e:
            logging.error(f"Error retrieving trades: {str(e)}")
            return {"error": str(e)}

        def clean_trade(trade):
            for key, value in trade.items():
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    trade[key] = None
                elif value is None:
                    trade[key] = None  # Replace None with a safe default (can be None if needed)
            return trade

        trades_list = trades_sql  # + trades_pgs
        if not trades_list:
            return {"data": []}

        safe_trades_list = [clean_trade(trade) for trade in trades_list]
        self.postgres_db.save_trades(safe_trades_list)
        return {"data": safe_trades_list}

    def list_folders(self, base_path: str, directory: str) -> List[str]:
        dir_path = os.path.join(base_path, directory)
        return [d for d in os.listdir(dir_path) if os.path.isdir(os.path.join(dir_path, d))]

    def get_local_databases(self):
        base_path = "bots"
        active_bots_path = os.path.join(base_path, "instances")
        active_bots_instances = self.list_folders(base_path, "instances")
        active_bots_databases = []
        for active_bots_instance in active_bots_instances:
            db_path = os.path.join(active_bots_path, active_bots_instance, "data")
            active_bots_databases += [os.path.join(db_path, db_file) for db_file in
                                      os.listdir(db_path)
                                      if db_file.endswith(".sqlite") and
                                      db_file.startswith("trade-controller-")]
        return active_bots_databases


# Instantiate the manager
emporus_manager = EmporusTradeManager()


@router.get("/emporus-databases", response_model=List[str])
async def emporus_databases():
    active_databases = emporus_manager.get_local_databases()
    if not active_databases:
        return {"error": "No databases found"}
    return active_databases


@router.get("/emporus-trades", response_model=Dict[str, Any])
async def emporus_trades(start_time: int = None, end_time: int = None):
    return emporus_manager.get_trades(start_time, end_time)


@router.get("/get-instance-config/{container_name}", response_model=dict)
async def get_instance_config(container_name: str):
    # Form the instance directory path correctly
    instance_dir = os.path.join('bots', 'instances', container_name)

    # reading the instance_id from the conf/conf_client.yml
    conf_client_path = os.path.join(instance_dir, 'conf', 'conf_client.yml')
    with open(conf_client_path, 'r') as conf_client:
        data = yaml.safe_load(conf_client)
        instance_id = data.get('instance_id', None)
    if not instance_id:
        return {"error": "Instance ID not found in conf_client.yml"}

    # reading the controllers_config from the script config (conf/scripts/[instance_id].yml)
    instance_id = instance_id.replace("hummingbot-", "")
    script_config_path = os.path.join(instance_dir, 'conf', 'scripts', f"{instance_id}.yml")
    with open(script_config_path, 'r') as script_config:
        script_config = yaml.safe_load(script_config)
        controllers_config = script_config.get('controllers_config', None)
    if not controllers_config:
        return {"error": "controllers_config not found in script config"}

    # reading all the configs from the controllers_config (conf/controllers/[controller_name].yml)
    controller_configs = []
    for controller_name in controllers_config:
        controller_config_path = os.path.join(instance_dir, 'conf', 'controllers', f"{controller_name}")
        with open(controller_config_path, 'r') as controller_config:
            data = yaml.safe_load(controller_config)
            controller_configs.append(data)

    # return the script config and controller configs
    return {"script_config": script_config, "controller_configs": controller_configs}
