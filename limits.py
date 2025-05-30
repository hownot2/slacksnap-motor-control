import time
import sys
import RPi.GPIO as GPIO

GPIO.setmode(GPIO.BCM)

GPIO.setup(19, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(20, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(21, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# 1 = optical path blocked, 0 = open

def test():
    while True:
        try:
            print(
                'GPIO19:', GPIO.input(19),
                'GPIO20:', GPIO.input(20),
                'GPIO21:', GPIO.input(21),
            )

            time.sleep(1)
        except KeyboardInterrupt as e:
            sys.exit()


if __name__ == '__main__':
    test()
