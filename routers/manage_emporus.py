import logging
import math
import os
from typing import Any, Dict

import pandas as pd
import yaml
from fastapi import APIRouter
from sqlalchemy import create_engine

router = APIRouter(tags=["Emporus Management"])


class EmporusTradeManager:
    def __init__(self):
        username = os.getenv("POSTGRES_USERNAME", "postgres")
        password = os.getenv("POSTGRES_PASSWORD", "postgres")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = int(os.getenv("POSTGRES_PORT", 5432))
        database = os.getenv("POSTGRES_DATABASE", "hummingbot")
        db_connection_string = f"postgresql://{username}:{password}@{host}:{port}/{database}"

        self.engine = create_engine(
            db_connection_string,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            connect_args={"connect_timeout": 10}
        )

    def get_trades(self, start_time: int = None, end_time: int = None) -> Dict[str, Any]:
        try:
            request_query = "SELECT * FROM \"EmporusTrades\" WHERE 1=1"
            query_params = []

            if start_time:
                request_query += " AND \"update_timestamp\" > %s"
                query_params.append(start_time)

            if end_time:
                request_query += " AND \"update_timestamp\" < %s"
                query_params.append(end_time)

            with self.engine.connect() as connection:
                trades_list = pd.read_sql_query(request_query, connection.connection, params=query_params).to_dict('records')

                def clean_trade(trade):
                    for key, value in trade.items():
                        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                            trade[key] = None
                        elif value is None:
                            trade[key] = None  # Replace None with a safe default (can be None if needed)
                    return trade

                safe_trades_list = [clean_trade(trade) for trade in trades_list]
                return {"data": safe_trades_list}
        except Exception as e:
            logging.error(f"Error retrieving trades: {str(e)}")
            return {"error": str(e)}


# Instantiate the manager
emporus_manager = EmporusTradeManager()


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
