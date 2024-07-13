import asyncio
import json
import time
import aiomqtt
import argparse
import os
import logging
from datetime import datetime


# see https://github.com/goecharger/go-eCharger-API-v2/blob/main/apikeys-en.md


CHARGER_KEYS_FOR_INFO_DISPLAY = sorted(
    ['ids/set', 'nrg', 'modelStatus', 'fup', 'ama', 'lmo','sh','psh'])

charger_states = {}

powerflow = {}

logger: logging.Logger


def init_logger(opts):
    # Setup logger
    logger = logging.getLogger()
    log_level = getattr(logging, opts.log_level.upper())
    logger.setLevel(log_level)

    # Create a handler for stdout
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(log_level)

    # Create a formatter and add it to the handler
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stdout_handler.setFormatter(formatter)

    # Add the handler to the logger
    logger.addHandler(stdout_handler)

    return logger


async def run_mqtt_loop(opts):

    async def process_powerflow_message(opts, client, message):
        global powerflow
        payload = json.loads(message.payload.decode())
        logger.debug(f"Received powerflow message: {payload}")
        powerflow = payload.copy()
        powerflow['timestamp'] = datetime.now().isoformat()
        grid_power = payload['grid']['power']
        pv_power = payload['inverter']['pv_production']
        for charger_queue in opts.charger_queues:
            try:
                message = {"pGrid": -grid_power, "pPv": pv_power}
                ids_topic = charger_queue + "/ids/set"
                await client.publish(ids_topic, json.dumps(message))
            except Exception as e:
                logger.error("Error: ", e)

    async def process_charger_message(opts, client, message):
        logger.debug(
            f"Received charger topic {message.topic}: {message.payload.decode()}")
        for charger_key in opts.charger_queues:
            if str(message.topic).startswith(charger_key):
                payload = message.payload.decode()
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError as e:
                    pass
                sub_topic = str(message.topic).replace(
                    charger_key + "/", "", 1)
                charger_states[charger_key][sub_topic] = payload

    async def run_loop():

        logger.info("Starting MQTT loop")

        async with aiomqtt.Client(
                hostname=opts.mqtt_host,
                username=opts.mqtt_user,
                password=opts.mqtt_password) as client:

            await client.subscribe(opts.solaredge_powerflow_queue)

            for charger_queue in opts.charger_queues:
                charger_states[charger_queue] = {}
                await client.subscribe(charger_queue + "/#")

            async for message in client.messages:
                if str(message.topic) == opts.solaredge_powerflow_queue:
                    await process_powerflow_message(opts, client, message)
                elif str(message.topic).startswith(tuple(opts.charger_queues)):
                    await process_charger_message(opts, client, message)
                else:
                    logger.warning(
                        f"Unexpected message on topic {message.topic}: {message.payload}")

        logger.warning("MQTT loop ended unexpectedly. Exiting ...")

        # Stop all tasks in the event loop and exit process
        tasks = asyncio.all_tasks()
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        loop.stop()
        loop.close()
        return

    await run_loop()


async def state_info_loop(opts):
    logger.info("Starting state info loop")
    while True:
        logger.info(f"Last powerflow: {powerflow}")
        for k, v in charger_states.items():
            selected_values = {k: v[k]
                               for k in CHARGER_KEYS_FOR_INFO_DISPLAY if k in v}
            logger.info(
                f"Charger {k} state: {selected_values}")
        await asyncio.sleep(60)


def parse_args():

    class JsonAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            try:
                setattr(namespace, self.dest, json.loads(values))
            except json.JSONDecodeError as e:
                raise argparse.ArgumentTypeError(f"Invalid JSON: {e}")

    parser = argparse.ArgumentParser(
        description='Powerflow to go-eCharger surplus charging')

    parser.add_argument('--mqtt-host', type=str,
                        default=os.environ.get('MQTT_HOST', None),
                        help='MQTT_HOST broker hostname')

    parser.add_argument('--mqtt-user', type=str,
                        default=os.environ.get(
                            'MQTT_USER', None),
                        help='MQTT_USER broker username')

    parser.add_argument('--mqtt-password', type=str,
                        default=os.environ.get("MQTT_PASSWORD"),
                        help='MQTT_PASSWORD broker password')

    parser.add_argument('--solaredge-powerflow-queue', type=str,
                        default=os.environ.get(
                            "SOLAREDGE_POWERFLOW_QUEUE", "solaredge/powerflow"),
                        help='Queue to listen for solaredge powerflow messages (default: "solaredge/powerflow")')

    parser.add_argument('--charger-queues', type=str,
                        default=json.loads(
                            os.environ.get("CHARGER_QUEUES", '[]')),
                        action=JsonAction,
                        help='json parseable list of e-go charger queues (without trailing /) to monitor (default: "[]")')

    parser.add_argument('--log-level', type=str,
                        default=os.environ.get('LOG_LEVEL', 'INFO'),
                        help='Logging level (default: "INFO")')

    parser.add_argument('--run-info-loop', action=argparse.BooleanOptionalAction, default=True,
                        help='Flag to run the state_info_loop (default: True)')

    return parser.parse_args()


opts = parse_args()
print("opts: ", opts, opts.mqtt_host, opts.mqtt_user, opts.mqtt_password)
logger = init_logger(opts)

loop = asyncio.get_event_loop()
loop.create_task(run_mqtt_loop(opts))
if opts.run_info_loop:
    loop.create_task(state_info_loop(opts))
loop.run_forever()
