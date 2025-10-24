import time
import sys
import RPi.GPIO as GPIO


PATH_OPEN = 0
PATH_BLOCKED = 1
LIMIT_LOWER_RIGHT = 19
LIMIT_LOWER_LEFT = 20
LIMIT_UPPER_RIGHT = 21


GPIO.setmode(GPIO.BCM)

GPIO.setup(LIMIT_LOWER_RIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(LIMIT_LOWER_LEFT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(LIMIT_UPPER_RIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)


# True = optical path blocked, False = open

def test():
    while True:
        try:
            print(
                'Lower Right (GPIO19):', GPIO.input(LIMIT_LOWER_RIGHT) == PATH_BLOCKED,
                '| Lower Left (GPIO20):', GPIO.input(LIMIT_LOWER_LEFT) == PATH_BLOCKED,
                '| Upper Right (GPIO21):', GPIO.input(LIMIT_UPPER_RIGHT) == PATH_BLOCKED,
            )

            time.sleep(1)
        except KeyboardInterrupt as e:
            sys.exit()


if __name__ == '__main__':
    test()
