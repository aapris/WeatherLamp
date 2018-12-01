"""
Pikkujouluvekotin
~~~~~~~~~~~~~~~~~

A user interface to control Pikkujoulu widget lights,
written with Flask.

:copyright: (c) 2018 by Aapo Rista.
:license: MIT, see LICENSE for more details.
"""

import json
import os
import random

from flask import (Flask, Response, request, redirect, url_for, render_template, send_from_directory)
from flask_socketio import SocketIO
from flask_socketio import join_room, leave_room, rooms
from flask_mqtt import Mqtt
from flask_bootstrap import Bootstrap
import eventlet


# Colorful error and debug prints
def print_error(skk):
    print("\033[91m {}\033[00m".format(skk))


def debug(skk):
    if DEBUG:
        print("\033[92m {}\033[00m".format(skk))


try:
    from .pikkujoulu_ui_config import MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, SECRET_KEY
except ImportError as err:
    print_error('\n\nNo pikkujoulu_ui_config found. Did you remember to')
    print_error('    cp pikkujoulu_ui_config_example.py pikkujoulu_ui_config.py')
    print_error('and set the variables correctly?\n\n\n')
    raise

# configuration
DEBUG = True
eventlet.monkey_patch()

# Create application
app = Flask('pikkujoulu_ui')
app.config.from_object(__name__)
app.config.from_envvar('PIKKUJOULU_SETTINGS', silent=True)
app.config['SECRET_KEY'] = SECRET_KEY

# MQTT config
app.config['MQTT_BROKER_URL'] = MQTT_HOST
app.config['MQTT_BROKER_PORT'] = MQTT_PORT
app.config['MQTT_USERNAME'] = MQTT_USER
app.config['MQTT_PASSWORD'] = MQTT_PASS
app.config['MQTT_KEEPALIVE'] = 5  # set the time interval for sending a ping to the broker to 5 seconds
app.config['MQTT_TLS_ENABLED'] = False  # set TLS to disabled for testing purposes

socketio = SocketIO(app)
mqtt = Mqtt(app)
bootstrap = Bootstrap(app)

# MQTT stuff
topic = 'led/#'
topic2 = 'led/ctrl'
port = 1883


def get_mqtt_data(message):
    topic = message.topic
    payload = message.payload.decode()
    data = dict(
        topic=topic,
        payload=payload
    )
    return data


@mqtt.on_connect()
def handle_connect(client, userdata, flags, rc):
    mqtt.subscribe(topic)
    mqtt.publish('led/server', b'Hello world, a server has started.')
    debug("MQTT connected")


@mqtt.on_message()
def handle_mqtt_message(client, userdata, message):
    data = get_mqtt_data(message)
    e = 'debug'
    data['type'] = e
    t = data['topic'].split('/')
    if data['topic'].startswith('led/ping/'):
        debug("Got MQTT message with ping topic")
        pass
    room = 'NoRoom'
    if len(t) > 1 and t[1] == 'ping':
        e = 'ping'
        data['dev'] = t[-1]
        data['type'] = e
    if len(t) == 4:
        room = t[2]
    if room == 'NoRoom':
        socketio.emit("debug", data=data)
        return
    socketio.emit(e, data=data, room=room)


# Web socket stuff

@socketio.on('connect')
def handle_connection_event():
    room = request.args.get('roomName')  # defined in client's io.connect query parameter
    join_room(room)
    debug('Client connected and joined to room {}'.format(room))
    data = {"info": "Connection ok, joined to {}".format(room)}
    socketio.emit("debug", data=data, room=room)


@socketio.on('ledclick')
def handle_ledclick_event(json):
    debug('received json: ' + str(json))
    client_rooms = [x for x in rooms() if len(x) < 30]
    if len(client_rooms) == 1:
        room = client_rooms[0]
    else:
        room = 'ERROR_IN_ROOM'
    topic = '{}/{}/{}'.format(topic2, room, json['dev'])
    random_mode = '{}'.format(random.randint(0, 5)).encode()  # returns byte '0'..'5'
    msg = b'00' + random_mode + b'\xff\x00\x00'
    mqtt.publish(topic, msg)


@socketio.on('buttonclick')
def handle_my_custom_event(json):
    debug('received json: ' + str(json))


@app.route('/')
def index():
    modes = []
    return render_template('index.html', messages=modes)


@app.route('/room/<path:roomname>')
def room(roomname):
    if len(roomname) > 20:
        return redirect(url_for('room', roomname=roomname[:20]))
    modes = []
    return render_template('index.html', messages=modes, roomname=roomname)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')
