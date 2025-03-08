import os
import logging
logging.basicConfig(
        format='[%(asctime)s] %(levelname)s %(module)s/%(funcName)s - %(message)s',
        level=logging.DEBUG if os.environ.get('DEBUG') else logging.INFO)

logging.info('')
logging.info('BOOT UP')

import asyncio
import aiomqtt
import serial
import glob
import time

import dyn4
import relays

ENCODER_PPR = 65536

ONE_MOTOR = os.environ.get('ONE_MOTOR', False)

dmm1 = None
dmm2 = None


MOTOR_EN1 = 1
MOTOR_EN2 = 2
DISABLED = None

def enable_motors():
    global DISABLED

    logging.info('Enabling both motors...')
    relays.relay_on(MOTOR_EN1)
    relays.relay_on(MOTOR_EN2)

    DISABLED = False

def disable_motors():
    global DISABLED

    logging.info('Disabling both motors...')
    relays.relay_off(MOTOR_EN1)
    relays.relay_off(MOTOR_EN2)

    DISABLED = True


async def send_mqtt(topic, message):
    try:
        async with aiomqtt.Client('localhost') as client:
            await client.publish(topic, payload=message.encode())
    except BaseException as e:
        logging.error('Problem sending MQTT topic %s, message %s: %s - %s', topic, message, e.__class__.__name__, e)
        await asyncio.sleep(1)
        return False


def set_motors(rpm):
    # we want these to happen no matter what so motors stay in sync

    logging.debug('Setting motor RPMs to %s', rpm)

    if dmm1:
        try:
            dmm1.set_speed(rpm)
        except BaseException as e:
            logging.error('Problem setting Motor1 rpm %s: %s - %s', rpm, e.__class__.__name__, e)

    if dmm2:
        try:
            dmm2.set_speed(rpm)
        except BaseException as e:
            logging.error('Problem setting Motor2 rpm %s: %s - %s', rpm, e.__class__.__name__, e)


async def process_mqtt(message):
    try:
        text = message.payload.decode()
    except UnicodeDecodeError:
        return

    topic = message.topic.value
    logging.debug('MQTT topic: %s, message: %s', topic, text)

    if topic == 'motion/set_rpm':
        rpm = int(float(text))
        set_motors(rpm)


async def monitor_mqtt():
    await asyncio.sleep(1)

    # from https://sbtinstruments.github.io/aiomqtt/reconnection.html
    # modified to make new client since their code didn't work
    # https://github.com/sbtinstruments/aiomqtt/issues/269
    while True:
        try:
            async with aiomqtt.Client('localhost') as client:
                await client.subscribe('motion/#')
                async for message in client.messages:
                    await process_mqtt(message)
        except aiomqtt.MqttError:
            logging.info('MQTT connection lost, reconnecting in 1 second...')
            await asyncio.sleep(1)


async def init_motor(path, name):
    try:
        dmm = dyn4.DMMDrive(path, 0)
        _ =  dmm.read_AbsPos32()
    except BaseException as e:
        logging.error('Problem opening %s port %s: %s - %s', name, path, e.__class__.__name__, e)
        await send_mqtt('server/motor_status', name + ' disconnected')
        await asyncio.sleep(1)
        return False

    logging.info('Port %s connected.', path)
    await send_mqtt('server/motor_status', name + ' connected')

    dmm.set_speed(0)

    return dmm


async def read_motor(dmm, name):
    try:
        return dmm.read_AbsPos32()
    except BaseException as e:
        # any problems, kill both motors
        set_motors(0)
        disable_motors()

        logging.error('Problem reading %s: %s - %s', name, e.__class__.__name__, e)
        await send_mqtt('server/motor_status', name + ' disconnected')
        dmm = False
        return False


async def check_sync(rev1, rev2):
    global dmm1, dmm2

    difference = rev1 - rev2

    if abs(difference) > 1.0:
        # out of sync, kill both motors
        set_motors(0)
        disable_motors()

        dmm1 = False
        dmm2 = False

        logging.error('MOTOR READINGS OUT OF SYNC! Motor1: %s, Motor2: %s, difference: %s', rev1, rev2, difference)
        await send_mqtt('server/motor_status', 'Motor desync')


async def monitor_dyn4():
    global dmm1, dmm2

    while True:
        await asyncio.sleep(0.1)

        if not dmm1:
            dmm1 = await init_motor('/dev/ttyUSB0', 'Motor1')
            continue

        if not dmm2 and not ONE_MOTOR:
            dmm2 = await init_motor('/dev/ttyUSB1', 'Motor2')
            continue

        pos1 = await read_motor(dmm1, 'Motor1')
        if pos1 is False:
            dmm1 = False
            continue
        rev1 = pos1 / ENCODER_PPR

        if ONE_MOTOR:
            revolutions = rev1
        else:
            pos2 = await read_motor(dmm2, 'Motor2')
            if pos2 is False:
                dmm2 = False
                continue
            rev2 = pos2 / ENCODER_PPR

            revolutions = (rev1 + rev2) / 2.0

            await check_sync(rev1, rev2)

        topic = 'server/position'
        message = str(round(revolutions, 5))
        await send_mqtt(topic, message)


        if ONE_MOTOR:
            if DISABLED is True and dmm1:
                logging.info('Motor1 communication back, re-enabling...')
                enable_motors()
        else:
            if DISABLED is True and dmm1 and dmm2:
                logging.info('Motor1 and Motor2 communication back, re-enabling...')
                enable_motors()



def task_died(future):
    if os.environ.get('SHELL'):
        logging.error('Motion control task died!')
    else:
        logging.error('Motion control task died! Waiting 60s and exiting...')
        time.sleep(60)
    exit()


def main():
    enable_motors()
    time.sleep(2)

    loop = asyncio.get_event_loop()

    a = loop.create_task(monitor_dyn4()).add_done_callback(task_died)
    b = loop.create_task(monitor_mqtt()).add_done_callback(task_died)

    loop.run_forever()


if __name__ == '__main__':
    main()
