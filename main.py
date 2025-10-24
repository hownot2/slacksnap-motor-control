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
import RPi.GPIO as GPIO

import dyn4
import relays
import limits

ENCODER_PPR = 65536

ONE_MOTOR = os.environ.get('ONE_MOTOR', False)

dmm1 = None
dmm2 = None


# 2025-10-24 facts:
#    Motor1 is left
#    Motor2 is right
#    Positive RPM moves the crosshead down
#    Negative RPM moves the crosshead up


MOTOR_EN1 = 1
MOTOR_EN2 = 2
DISABLED = None
RPM_TARGET = 0

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
    global RPM_TARGET

    try:
        text = message.payload.decode()
    except UnicodeDecodeError:
        return

    topic = message.topic.value
    logging.debug('MQTT topic: %s, message: %s', topic, text)

    if topic == 'motion/set_rpm':
        rpm = int(float(text))
        RPM_TARGET = rpm


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
    global RPM_TARGET

    try:
        return dmm.read_AbsPos32()
    except BaseException as e:
        # any problems, kill both motors
        RPM_TARGET = 0
        set_motors(0)
        disable_motors()

        logging.error('Problem reading position of %s: %s - %s', name, e.__class__.__name__, e)
        await send_mqtt('server/motor_status', name + ' disconnected')
        dmm = False
        return False

async def check_status(dmm, name):
    global RPM_TARGET

    try:
        status = dmm.read_Status()
        current = dmm.read_TrqCurrent()
        logging.info('%s status, alarm: %s, current: %s', name, status['alarm'] or 'None', current)
    except BaseException as e:
        # any problems, kill both motors
        RPM_TARGET = 0
        set_motors(0)
        disable_motors()

        logging.error('Problem reading status of %s: %s - %s', name, e.__class__.__name__, e)
        await send_mqtt('server/motor_status', name + ' disconnected')
        dmm = False
        return False


async def check_sync(rev1, rev2):
    global dmm1, dmm2, RPM_TARGET

    difference = rev1 - rev2

    if abs(difference) > 2.0:
        # out of sync, kill both motors
        RPM_TARGET = 0
        set_motors(0)
        disable_motors()

        dmm1 = False
        dmm2 = False

        logging.error('MOTOR READINGS OUT OF SYNC! Motor1: %s, Motor2: %s, difference: %s', rev1, rev2, difference)
        await send_mqtt('server/motor_status', 'Motor desync')

        return False

    return True


async def monitor_dyn4():
    global dmm1, dmm2, RPM_TARGET

    while True:
        await asyncio.sleep(0.1)

        if not dmm1:
            dmm1 = await init_motor('/dev/ttyUSB0', 'Motor1')
            continue

        if not dmm2 and not ONE_MOTOR:
            dmm2 = await init_motor('/dev/ttyUSB1', 'Motor2')
            continue

        if dmm1:
            await check_status(dmm1, 'Motor1')

        if dmm2:
            await check_status(dmm2, 'Motor2')

        pos1 = await read_motor(dmm1, 'Motor1')
        if pos1 is False:
            dmm1 = False
            continue
        rev1 = round(pos1 / ENCODER_PPR, 4)

        if ONE_MOTOR:
            revolutions = rev1
            motor1_rpm = RPM_TARGET
        else:
            pos2 = await read_motor(dmm2, 'Motor2')
            if pos2 is False:
                dmm2 = False
                continue
            rev2 = round(pos2 / ENCODER_PPR, 4)

            revolutions = (rev1 + rev2) / 2.0

            if not await check_sync(rev1, rev2):
                continue

            motor1_scaler = 1.0
            motor2_scaler = 1.0
            kp = 0.01
            difference = rev1 - rev2

            #if abs(difference) > 0.25:
            #    if RPM_TARGET > 0:
            #        if rev1 > rev2:
            #            motor1_scaler = 1.0 - kp * abs(difference)
            #            motor1_scaler = min(motor1_scaler, 1.0)  # clamp to range 0.5 - 1.0
            #            motor1_scaler = max(motor1_scaler, 0.5)
            #        elif rev2 > rev1:
            #            motor2_scaler = 1.0 - kp * abs(difference)
            #            motor2_scaler = min(motor2_scaler, 1.0)  # clamp to range 0.5 - 1.0
            #            motor2_scaler = max(motor2_scaler, 0.5)
            #    elif RPM_TARGET < 0:
            #        if rev1 < rev2:
            #            motor1_scaler = 1.0 - kp * abs(difference)
            #            motor1_scaler = min(motor1_scaler, 1.0)  # clamp to range 0.5 - 1.0
            #            motor1_scaler = max(motor1_scaler, 0.5)
            #        elif rev2 < rev1:
            #            motor2_scaler = 1.0 - kp * abs(difference)
            #            motor2_scaler = min(motor2_scaler, 1.0)  # clamp to range 0.5 - 1.0
            #            motor2_scaler = max(motor2_scaler, 0.5)

            motor1_rpm = int(RPM_TARGET * motor1_scaler)
            motor2_rpm = int(RPM_TARGET * motor2_scaler)

            if abs(difference) > 0.25:
                logging.info('Pos difference: %s, rev1: %s, rev2: %s, scaler1: %s, scaler2: %s, target: %s, rpm1: %s, rpm2: %s',
                    difference, rev1, rev2, motor1_scaler, motor2_scaler, RPM_TARGET, motor1_rpm, motor2_rpm)


        # check limit switches
        if ONE_MOTOR:
            pass
        else:
            GOING_DOWNWARD = motor1_rpm > 0 or motor2_rpm > 0
            GOING_UPWARD = motor1_rpm < 0 or motor2_rpm < 0

            if GOING_DOWNWARD:
                if GPIO.input(limits.LIMIT_LOWER_RIGHT) == limits.PATH_BLOCKED or GPIO.input(limits.LIMIT_LOWER_LEFT) == limits.PATH_BLOCKED:
                    motor1_rpm = 0
                    motor2_rpm = 0
                    logging.info('Lower limit switch hit, preventing downward travel.')

            if GOING_UPWARD:
                if GPIO.input(limits.LIMIT_UPPER_RIGHT) == limits.PATH_BLOCKED:
                    motor1_rpm = 0
                    motor2_rpm = 0
                    logging.info('Upper limit switch hit, preventing upward travel.')


        if ONE_MOTOR:
            logging.debug('Setting motor1: %s', motor1_rpm)
            dmm1.set_speed(motor1_rpm)
        else:
            logging.debug('Setting motor1: %s, motor2: %s', motor1_rpm, motor2_rpm)
            dmm1.set_speed(motor1_rpm)
            dmm2.set_speed(motor2_rpm)


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
