import json
import logging
import math
import os
from typing import Any, Dict, List, Mapping

import pandas as pd
import yaml
from emporus.cex.db.postgres import EmporusPostgresDatabase
from fastapi import APIRouter
from sqlalchemy import create_engine

router = APIRouter(tags=["Emporus Management"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class EmporusSQLiteDatabase:
    def __init__(self, db_path: str):
        self.db_name = os.path.basename(db_path)
        self.db_path = db_path
        self.db_path = f'sqlite:///{os.path.join(db_path)}'
        self.engine = create_engine(self.db_path, connect_args={'check_same_thread': False})

    def get_trades(self, query: str, params: list[Any] | Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
        with self.engine.connect() as connection:
            trades_list = pd.read_sql_query(query, connection.connection, params=params).to_dict('records')
            logger.info(f"Fetched {len(trades_list)} trades from {self.db_path}")
        # Convert stringified JSON fields to JSON
        for trade in trades_list:
            for key in ["entry_details", "exit_details", "raw_model_output"]:
                if key in trade and isinstance(trade[key], str):
                    trade[key] = json.loads(trade[key])
        return trades_list

    def dispose(self):
        if self.engine:
            self.engine.dispose()
            self.engine = None


class EmporusTradeManager:
    def __init__(self):
        self.postgres_db = EmporusPostgresDatabase()
        self.sqlite_dbs: Dict[str, EmporusSQLiteDatabase] = {}
        logger.info("EmporusTradeManager initialized")

    def get_trades_from_sqlite(self, query: str, params: list[Any] | Mapping[str, Any] | None = None) -> List[Dict[str, Any]]:
        logger.info("Available SQLite DBs:")
        for path in self.sqlite_dbs:
            logger.info(f" * {path} ({'exists' if os.path.exists(path) else 'removed'})")

        for db_path in self.get_local_databases():
            if db_path not in self.sqlite_dbs:
                logger.info(f"Adding SQLite DB: {db_path}")
                self.sqlite_dbs[db_path] = EmporusSQLiteDatabase(db_path)

        # Remove archived DBs
        invalid_paths = [path for path in self.sqlite_dbs if not os.path.exists(path)]
        for path in invalid_paths:
            logger.info(f"Removing archived SQLite DB: {path}")
            self.sqlite_dbs[path].dispose()
            del self.sqlite_dbs[path]

        trades_list = []
        query = query.replace("%s", "?")
        for db in self.sqlite_dbs.values():
            trades_list += db.get_trades(query, params)
        return trades_list

    def clean_trade(self, trade):
        for key, value in trade.items():
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                trade[key] = None
            elif value is None:
                trade[key] = None  # Replace None with a safe default (can be None if needed)
        return trade

    def emporus_sync_and_fetch_trades(self, start_time: int = None, end_time: int = None) -> Dict[str, Any]:
        request_query = "SELECT * FROM \"EmporusTrades\" WHERE 1=1"
        query_params = []

        if start_time:
            request_query += " AND \"update_timestamp\" > %s"
            query_params.append(start_time)

        if end_time:
            request_query += " AND \"update_timestamp\" < %s"
            query_params.append(end_time)

        try:
            trades_pgs = self.postgres_db.get_trades(request_query, query_params)
            trades_sql = self.get_trades_from_sqlite(request_query, query_params)
        except Exception as e:
            logger.error(f"Error retrieving trades: {str(e)}")
            return {"error": str(e)}

        if not trades_sql and not trades_pgs:
            return {"data": []}

        duplicated_ids = [t['id'] for t in trades_sql if t['id'] in [t['id'] for t in trades_pgs]]
        logger.info(f"Retrieved {len(trades_pgs)} trades from Postgres and {len(trades_sql)} trades"
                    f" from SQLite ({len(duplicated_ids)} duplicates)")

        # Convert trades to a safe format for JSON serialization
        trades_pgs = [self.clean_trade(trade) for trade in trades_pgs]
        trades_sql = [self.clean_trade(trade) for trade in trades_sql if trade['id'] not in duplicated_ids]

        # Save new trades to Postgres
        self.postgres_db.save_trades(trades_sql)

        # Return the combined list of trades
        return {"data": trades_pgs + trades_sql}

    def sqlite_to_postgres(self, instance_name: str):
        # Get the SQLite database path
        db_path = self.get_instance_db_path(instance_name)

        logger.info(f"Fetching trades from {db_path}")
        db = self.sqlite_dbs.get(db_path, EmporusSQLiteDatabase(db_path))
        trades_list = db.get_trades("SELECT * FROM \"EmporusTrades\"")

        # Save trades to Postgres
        self.postgres_db.save_trades(trades_list)

        logger.info(f"Disposing SQLite DB: {db_path}")
        db.dispose()
        if db_path in self.sqlite_dbs:
            del self.sqlite_dbs[db_path]

    def get_instance_db_path(self, instance_name: str) -> str:
        base_path = "bots"
        active_bots_path = os.path.join(base_path, "instances")
        # look for the .sqlite file in the data directory of the instance
        db_path = os.path.join(active_bots_path, instance_name, "data")
        db_files = [db_file for db_file in os.listdir(db_path) if db_file.endswith(".sqlite") and
                    db_file.startswith("trade-controller-")]
        if not db_files:
            raise Exception(f"No .sqlite file found in {db_path}")
        return os.path.join(db_path, db_files[0])

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


@router.post("/emporus-save-trades/{container_name}")
async def emporus_save_trades(container_name: str):
    try:
        emporus_manager.sqlite_to_postgres(container_name)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/emporus-sync-and-fetch-trades", response_model=Dict[str, Any])
async def emporus_sync_and_fetch_trades(start_time: int = None, end_time: int = None):
    return emporus_manager.emporus_sync_and_fetch_trades(start_time, end_time)


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
