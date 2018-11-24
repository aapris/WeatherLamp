"""
Pikkujouluvekotin
~~~~~~~~~~~~~~~~~

A user interface to control Pikkujoulu widget lights,
written with Flask.

:copyright: (c) 2018 by Aapo Rista.
:license: MIT, see LICENSE for more details.
"""

import json
import paho.mqtt.client as mqtt
from flask import (Flask, Response, render_template)
from flask_socketio import SocketIO
from flask_mqtt import Mqtt
from flask_bootstrap import Bootstrap
import eventlet

try:
    from .pikkujoulu_ui_config import MQTT_HOST, MQTT_PORT, SECRET_KEY
except ImportError as err:
    print('\n\nNo pikkujoulu_ui_config found. Did you remember to')
    print('    cp pikkujoulu_ui_config_example.py pikkujoulu_ui_config.py')
    print('and set the variables correctly?\n\n\n')
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
# app.config['MQTT_USERNAME'] = ''  # set the username here if you need authentication for the broker
# app.config['MQTT_PASSWORD'] = ''  # set the password here if the broker demands authentication
app.config['MQTT_KEEPALIVE'] = 5  # set the time interval for sending a ping to the broker to 5 seconds
app.config['MQTT_TLS_ENABLED'] = False  # set TLS to disabled for testing purposes

socketio = SocketIO(app)
mqtt = Mqtt(app)
bootstrap = Bootstrap(app)

# MQTT stuff
topic = '#'
topic2 = 'led/control'
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
    mqtt.publish('led/server', b'hello world')
    print("MQTT connected")


@mqtt.on_message()
def handle_mqtt_message(client, userdata, message):
    print("ON MQTT MESSAGE")
    data = get_mqtt_data(message)
    e = 'debug'
    data['type'] = e
    t = data['topic'].split('/')
    if data['topic'].startswith('led/ping/'):
        print("Got message with ping topic")
    if len(t) > 1 and t[1] == 'ping':
        e = 'ping'
        data['dev'] = t[2]
        data['type'] = e
    socketio.emit(e, data=data)


# Web socket stuff
@socketio.on('message')
def handle_message(message):
    print('SOCKETIO received message: ' + message)


@socketio.on('ledclick')
def handle_ledclick_event(json):
    print('received json: ' + str(json))
    topic = 'led/control/{}'.format(json['dev'])
    mqtt.publish(topic, b'00aa')


@socketio.on('buttonclick')
def handle_my_custom_event(json):
    print('received json: ' + str(json))


@app.route('/')
def index():
    modes = []
    return render_template('index.html', messages=modes)


@app.route('/modes')
def modes():
    json_modes = []
    json_modes.append({
        'id': '1',
        'url': '/foo/bar',
    })
    json_str = json.dumps(json_modes, indent=1)
    return Response(response=json_str, content_type='application/json')
