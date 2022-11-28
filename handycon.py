#!/usr/bin/env python3
# HandyGCCS HandyCon
# Copyright 2022 Derek J. Clark <derekjohn dot clark at gmail dot com>
# This will create a virtual UInput device and pull data from the built-in
# controller and "keyboard". Right side buttons are keyboard buttons that
# send macros (i.e. CTRL/ALT/DEL). We capture those events and send button
# presses that Steam understands.

import argparse
import asyncio
import configparser
import logging
import os
import platform
import re
import signal
import sys
import warnings

from constants import CONTROLLER_EVENTS, DETECT_DELAY, EVENT_ESC, EVENT_HOME, EVENT_OSK, EVENT_QAM, EVENT_SCR, FF_DELAY, HIDE_PATH, JOY_MAX, JOY_MIN
import evdev
# from evdev import InputDevice, InputEvent, UInput, ecodes, list_devices, ff
from pathlib import Path
from shutil import move
from time import sleep, time
import importlib
import yaml

logging.basicConfig(format="[%(asctime)s | %(filename)s:%(lineno)s:%(funcName)s] %(message)s",
                    datefmt="%y%m%d_%H:%M:%S",
                    level=logging.INFO)

logger = logging.getLogger()

# TODO: asyncio is using a deprecated method in its loop, find an alternative.
# Suppress for now to keep journalctl output clean.
warnings.filterwarnings("ignore", category=DeprecationWarning)


class HandyCon():
    states = ("initializing", "running", "halting", "shutdown")

    def __init__(self, cfg):

        self.cfg = cfg
        self.devices = dict()
        for k, v in self.cfg['devices'].items():
            # globals() is a crutch, but works for now.
            # Consider alternate means for matching classes specified in yaml
            self.devices[k] = globals()[v['class']](v)
            logger.debug(f"Found config for {k} with class {v['class']}")

        self.event_queue = []  # Stores incoming button presses to block spam
        self._id_system()
        Path(HIDE_PATH).mkdir(parents=True, exist_ok=True)
        # get_config()
        # make_controller()

    def __del__(self):
        # TODO: aggregate calls to mechanisms for graceful shutdown here
        pass

    def _id_system(self):
        """ Identify the current device type. Kill script if not compatible."""
        # Block devices that aren't supported as this could cause issues.
        if (system_id := open("/sys/devices/virtual/dmi/id/product_name", "r").read().strip()) not in self.cfg['compat']:
            logger.error(f"{system_id} is not currently supported by this tool." +
                         "Open an issue on GitHub at https://github.com/ShadowBlip/aya-neo-fixes if this is a bug." +
                         "If possible, please run the capture-system.py utility found on the GitHub repository and upload the generated file with your issue.")
            sys.exit(-1)
        logger.info(f"Identified host system as {system_id}; incompatible with {self.cfg['compat']}.")

    def get_device(self, label):
        try:
            if self.devices.get(label) is not None:
                logger.info(f"Found {label}. Capturing input data.")
                return True
            # Sometimes the service loads before all input devices have full initialized. Try a few times.
            else:
                logger.warn(f"Device {label} not yet found. Restarting scan.")
                sleep(self.cfg['system']['detect_delay'])
                return False
        # Some funky stuff happens sometimes when booting. Give it another shot.
        except Exception as err:
            logger.error(f"{err} | Error when scanning for {label}. Restarting scan.")
            sleep(self.cfg['system']['detect_delay'])
            return False

    async def capture_keyboard_events(self):
        """Captures keyboard events and translates them to virtual device events.
           Get access to global variables. These are globalized because the function
           is instanciated twice and need to persist accross both instances."""

        last_button = None

        # Capture keyboard events and translate them to mapped events.
        while self.state == 'running':
            if (keyboard := self.devices.get('keyboard')) is not None:
                try:
                    async for seed_event in keyboard.dev.async_read_loop():

                        # Loop variables
                        active = keyboard.dev.active_keys()
                        events = []
                        this_button = None
                        button_on = seed_event.value

                        # Debugging variables
                        # if active != []:
                        #     logging.debug(f"Active Keys: {keyboard.dev.active_keys(verbose=True)}, Seed Value: {seed_event.value}, Seed Code: {seed_event.code}, Seed Type: {seed_event.type}, Button pressed: {button_on}.")
                        # if event_queue != []:
                        #     logging.debug(f"Queued events: {event_queue}")

                        # Automatically pass default keycodes we dont intend to replace.
                        if seed_event.code in [evdev.ecodes.KEY_VOLUMEDOWN,
                                               evdev.ecodes.KEY_VOLUMEUP]:
                            events.append(seed_event)
                            for k, v in self.virtcon['events'].items():
                                logger.debug(k)
                                if all((active == [evdev.ecodes[ec] for ec in v['ecodes']],
                                        button_on == 1,
                                        self.virtcon.buttons[v['input']] not in self.event_queue)):
                                    event_queue.append(self.virtcon.buttons[v['input']])
                                elif all((active == [],
                                          seed_event.code in [evdev.ecodes[ec] for ec in v['ecodes']],
                                          button_on == 0,
                                          self.virtcon.buttons[v['input']] in self.event_queue)):
                                    # TODO: ACTION!
                                    pass

                        # Create list of events to fire.
                        # Handle new button presses.
                        if this_button and not last_button:
                            for button_event in this_button:
                                event = evdev.InputEvent(seed_event.sec,
                                                         seed_event.usec,
                                                         button_event[0],
                                                         button_event[1],
                                                         1)
                                events.append(event)
                            event_queue.remove(this_button)
                            last_button = this_button

                        # Clean up old button presses.
                        elif last_button and not this_button:
                            for button_event in last_button:
                                event = evdev.InputEvent(seed_event.sec,
                                                         seed_event.usec,
                                                         button_event[0],
                                                         button_event[1],
                                                         0)
                                events.append(event)
                            last_button = None

                        # Push out all events.
                        if events != []:
                            await self.emit_events(events)

                except Exception as err:
                    logger.error(f"{err} | Error reading events from {keyboard.dev.name}")
                    self.restore_device(keyboard)

            else:
                logger.info("Attempting to grab keyboard device...")
                self.get_keyboard()
                await asyncio.sleep(self.cfg['system']['detect_delay'])

    async def capture_gyro_events(self):
        while self.state == 'running':
            # Only run this loop if gyro is enabled
            if (gyro := self.devices.get('gyro')) is not None:
                if gyro.enabled:
                    # Periodically output the EV_ABS events according to the gyro readings.
                    x_adj = max(min(int(gyro.ang_vel_x() * gyro.sensitivity) + self.devices['controller'].dev.last_x_val, JOY_MAX), JOY_MIN)
                    x_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RX, x_adj)
                    y_adj = max(min(int(gyro.ang_vel_y() * gyro.sensitivity) + self.devices['controller'].dev.last_y_val, JOY_MAX), JOY_MIN)
                    y_event = evdev.InputEvent(0, 0, evdev.ecodes.EV_ABS, evdev.ecodes.ABS_RY, y_adj)

                    await self.emit_events([x_event, y_event])
                    await asyncio.sleep(0.01)
                else:
                    # Slow down the loop so we don't waste millions of cycles and overheat our controller.
                    await asyncio.sleep(0.5)

    # Captures power events and handles long or short press events.
    async def capture_powerkey_events(self):
        while self.state == 'running':
            if (powerkey := self.devices.get('powerkey')) is not None:
                try:
                    async for event in powerkey.dev.async_read_loop():
                        active_keys = self.devices['keyboard'].active_keys()
                        if all((event.type == evdev.ecodes.EV_KEY,
                                event.code == evdev.ecodes['KEY_POWER'])):  # KEY_POWER
                            if event.value == 0:
                                steam_path = self.home_path / '.steam/root/ubuntu12_32/steam'
                                if active_keys == [evdev.ecodes['KEY_LEFTMETA']]:
                                    # For DeckUI Sessions
                                    shutdown = True
                                    cmd = f'su {self.user} -c "{steam_path} -ifrunning steam://longpowerpress"'
                                    os.system(cmd)
                                else:
                                    # For DeckUI Sessions
                                    cmd = f'su {self.user} -c "{steam_path} -ifrunning steam://shortpowerpress"'
                                    os.system(cmd)

                                    # For BPM and Desktop sessions
                                    await asyncio.sleep(1)
                                    os.system('systemctl suspend')

                        if active_keys == [evdev.ecodes['KEY_LEFTMETA']]:
                            await do_rumble(0, 150, 1000, 0)

                except Exception as err:
                    logger.error(f"{err} | Error reading events from power device.")
                    self.devices['powerkey'] = None
            else:
                logger.info("Attempting to grab power device...")
                self.get_powerkey()
                await asyncio.sleep(self.cfg['system']['detect_delay'])

    async def capture_controller_events(self):
        while self.state == 'running':
            if (controller := self.devices.get('controller')) is not None:
                try:
                    async for event in controller.dev.async_read_loop():
                        # Block FF events, or get infinite recursion. Up to you I guess...
                        if event.type in [evdev.ecodes.EV_FF,
                                          evdev.ecodes.EV_UINPUT]:
                            continue

                        # If gyro is enabled, queue all events so the gyro event handler can manage them.
                        if (gyro := self.devices.get('gyro')) is not None and gyro.enabled:
                            adjusted_val = None

                            # We only modify RX/RY ABS events.
                            if event.type == evdev.ecodes['EV_ABS'] and event.code == evdev.ecodes.ABS_RX:
                                # Record last_x_val before adjustment.
                                # If right stick returns to the original position there is always an event that sets last_x_val back to zero.
                                controller.last_x_val = event.value
                                adjusted_val = max(min(int(gyro.ang_vel_x() * gyro.sensitivity) + event.value, JOY_MAX), JOY_MIN)
                            if event.type == evdev.ecodes['EV_ABS'] and event.code == evdev.ecodes.ABS_RY:
                                # Record last_y_val before adjustment.
                                # If right stick returns to the original position there is always an event that sets last_y_val back to zero.
                                controller.last_y_val = event.value
                                adjusted_val = max(min(int(gyro.ang_vel_y() * gyro.sensitivity) + event.value, JOY_MAX), JOY_MIN)

                            if adjusted_val:
                                # Overwrite the event.
                                event = evdev.InputEvent(event.sec, event.usec, event.type, event.code, adjusted_val)

                        # Output the event.
                        await self.emit_events([event])
                except Exception as err:
                    logger.error(f"{err} | Error reading events from {controller.dev.name}.")
                    self.restore_device(controller)
            else:
                logger.info("Attempting to grab controller device...")
                self.controller.get()
                await asyncio.sleep(self.cfg['system']['detect_delay'])

    # Handle FF event uploads
    async def capture_ff_events(self):
        ff_effect_id_set = set()

        async for event in self.virtcon.dev.async_read_loop():
            if (controller := self.devices['controller']) is None:
                # Slow down the loop so we don't waste millions of cycles and overheat our controller.
                await asyncio.sleep(.5)
                continue

            if event.type == evdev.ecodes.EV_FF:
                # Forward FF event to controller.
                controller.dev.write(evdev.ecodes.EV_FF,
                                     event.code,
                                     event.value)
                continue

            # Programs will submit these EV_UINPUT events to ensure the device is capable.
            # Doing this forever doesn't seem to pose a problem, and attempting to ignore
            # any of them causes the program to halt.
            if event.type != evdev.ecodes.EV_UINPUT:
                continue

            if event.code == evdev.ecodes.UI_FF_UPLOAD:
                # Upload to the virtual device to prevent threadlocking. This does nothing else
                upload = self.virtcon.dev.begin_upload(event.value)
                effect = upload.effect

                if effect.id not in ff_effect_id_set:
                    # Set to -1 for kernel to allocate a new id.
                    # All other values throw an error for invalid input.
                    effect.id = -1

                try:
                    # Upload to the actual controller.
                    effect_id = controller.dev.upload_effect(effect)
                    effect.id = effect_id

                    ff_effect_id_set.add(effect_id)

                    upload.retval = 0
                except IOError as err:
                    logger.error(f"{err} | Error uploading effect {effect.id}.")
                    upload.retval = -1

                self.virtcon.dev.end_upload(upload)

            elif event.code == evdev.ecodes.UI_FF_ERASE:
                erase = self.virtcon.dev.begin_erase(event.value)

                try:
                    controller.dev.erase_effect(erase.effect_id)
                    ff_effect_id_set.remove(erase.effect_id)
                    erase.retval = 0
                except IOError as err:
                    logger.error(f"{err} | Error erasing effect {erase.effect_id}.")
                    erase.retval = -1

                self.virtcon.dev.end_erase(erase)

    # Emits passed or generated events to the virtual controller.
    async def emit_events(self, events: list):
        if len(events) == 1:
            self.virtcon.dev.write_event(events[0])
            self.virtcon.dev.syn()

        elif len(events) > 1:
            for event in events:
                self.virtcon.dev.write_event(event)
                self.virtcon.dev.syn()
                await asyncio.sleep(self.virtcon.button_delay)

    def restore_device(self, device):
        device.dev = None
        device.event = None
        device.getattr('restore')()

    async def restore_all(self, loop):
        """Graceful shutdown."""
        logger.info("Receved exit signal. Restoring devices.")
        self.state = 'shutting down'
        for device in self.devices.values():
            self.restore_device(device)
        logger.info("Devices restored.")

        # Kill all tasks. They are infinite loops so we will wait forver.
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        loop.stop()
        logger.info("Handheld Game Console Controller Service stopped.")

    # Main loop
    def loop(self):
        # Attach the event loop of each device to the asyncio loop.
        for device in self.devices.keys():
            asyncio.ensure_future(self.getattr(f'capture_{device}_events')())

        # asyncio.ensure_future(self.capture_gyro_events())
        # asyncio.ensure_future(self.capture_keyboard_events())
        # asyncio.ensure_future(self.capture_powerkey_events())
        asyncio.ensure_future(self.capture_ff_events())

        logger.info("Handheld Game Console Controller Service started.")

        # Run asyncio loop to capture all events.
        loop = asyncio.get_event_loop()

        # Establish signaling to handle gracefull shutdown.
        for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(self.restore_all(loop)))

        try:
            loop.run_forever()
            exit_code = 0
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt.")
            exit_code = 1
        except Exception as err:
            logger.error(f"{err} | Hit exception condition.")
            exit_code = 2
        finally:
            loop.stop()
            sys.exit(exit_code)


# Capture the username and home path of the user who has been logged in the longest.
USER = None
HOME_PATH = Path('/home')
while USER is None:
    who = [w.split(' ', maxsplit=1) for w in os.popen('who').read().strip().split('\n')]
    who = [(w[0], re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})", w[1]).groups()[0]) for w in who]
    who.sort(key=lambda row: row[1])
    USER = who[0][0]
    sleep(.1)
logger.debug(f"USER: {USER}")
HOME_PATH /= USER
logger.debug(f"HOME_PATH: {HOME_PATH}")

# Functionality Variables
event_queue = []
running = True
shutdown = False

# Last right joystick X and Y value
# Holding the last value allows us to maintain motion while a joystick is held.
last_x_val = 0
last_y_val = 0


class VirtCon():
    def __init__(self, cfg=None):
        if cfg is None:
            return None
        self.cfg = cfg
        self.dev = evdev.UInput(**self.cfg['uinput'])
        self.buttons = {k: importlib.import_module(v) for k, v in self.cfg['buttons']}
        self.gyro = None
        self.controller = None
        self.keyboard = None
        self.powerkey = None


class Device():
    def __init__(self, cfg=None):
        if cfg is None:
            return None
        self.cfg = cfg
        self.dev = None

    def get(self):
        if self.cfg['path'] is not None:
            self.dev = evdev.InputDevice(self.cfg['path'])
        else:  # search
            for dev in [evdev.InputDevice(path) for path in evdev.util.list_devices()]:
                logger.debug(f"{dev.name}, {dev.phys}")
                if dev.name == self.cfg['driver']['name'] and dev.phys == self.cfg['driver']['phys']:
                    self.dev = dev
                    break
        if self.dev is not None:
            self.hide_path = Path(self.cfg.get('hide_path',
                                               Path(self.dev.path).parent / '.hide' / self.dev.name))
            self.hide_path.parent.mkdir(parents=True, exist_ok=True)
            self.dev.grab()
            move(self.dev.path, str(self.hide_path))
            return True
        else:
            return False

    def restore(self):
        # Both device threads will attempt this, so ignore if they have been moved.
        try:
            self.dev.ungrab()
        except IOError as err:
            logger.warn(f"{err} | Device wasn't grabbed.")
        self.restore()
        try:
            move(str(self.hide_path), self.dev.path)
        except FileNotFoundError:
            pass


class Keyboard(Device):
    def __init__(self, cfg=None):
        super().__init__(cfg)


class PowerKey(Device):
    def __init__(self, cfg=None):
        super().__init__(cfg)


class Controller(Device):
    def __init__(self, cfg):
        super().__init__(cfg)

    async def do_rumble(self, button=0, interval=10, length=1000, delay=0):
        # Create the rumble effect.
        rumble = evdev.ff.Rumble(strong_magnitude=0x0000, weak_magnitude=0xffff)
        effect = evdev.ff.Effect(evdev.ecodes.FF_RUMBLE,
                                 -1,
                                 0,
                                 evdev.ff.Trigger(button, interval),
                                 evdev.ff.Replay(length, delay),
                                 evdev.ff.EffectType(ff_rumble_effect=rumble))

        # Upload and transmit the effect.
        effect_id = self.dev.upload_effect(effect)
        self.dev.write(evdev.ecodes.EV_FF, effect_id, 1)
        await asyncio.sleep(interval / 1000)
        self.dev.erase_effect(effect_id)


class Gyro():
    def __init__(self, cfg):
        self.sensitivity = float(cfg.get('sensitivity', 1.0))
        self.scale = float(cfg.get('scale', 2000.0 / 32768.0))
        self.enabled = False
        try:
            driver = importlib.import_module(cfg['driver']['module'])
            self.dev = driver(**cfg['driver']['init_args'])
            logger.info("Found gyro device. Gyro support enabled.")
            for ax, v in cfg['driver']['ang_vel'].items():
                setattr(self, f'ang_vel_{ax}', lambda: self.scale * getattr(self.dev, v['method'])())
                if (index := v.get('index')):
                    setattr(self, f'ang_vel_{ax}', lambda: self.scale * getattr(self.dev, v['method'])()[index])
        except ModuleNotFoundError as err:
            logger.error(f"{err} | Gyro device not initialized. Skipping gyro device setup.")
            self.dev = None
        except (BrokenPipeError, FileNotFoundError, NameError, OSError) as err:
            logger.error(f"{err} | Gyro device not initialized. Ensure bmi160_i2c and i2c_dev modules are loaded. Skipping gyro device setup.")
            self.dev = None

    def toggle(self):
        self.enabled = not self.enabled

    def get(self):
        pass

    def restore(self):
        pass


class ForceFeedback():
    def __init__(self, cfg=None):
        pass

    def get(self):
        pass

    def restore(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--debug', '--verbose', '-v',
                        dest="verbose",
                        action='store_true',
                        help="Enable debug/verbose mode.")
    parser.add_argument('--user', '-u', nargs=1, type=str,
                        default="",
                        dest="user",
                        help="User name")
    parser.add_argument('--steam', nargs=1, type=str,
                        default="",
                        dest="steam_path",
                        help="Path to directory containing Steam executable.")
    parser.add_argument('--config', '-c', nargs='?', type=str,
                        default="config.yml",
                        dest="cfg_path",
                        help="Path to config file.")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logger.setLevel(log_level)

    with open(Path(args.cfg_path), 'r') as f:
        cfg = yaml.load(f, yaml.CLoader)

    hc = HandyCon(cfg)
    hc.loop()
