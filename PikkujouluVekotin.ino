/**************************************************************************************
   Sketch to blink RGB LED strip in Finnish pre-christmas party called Pikkujoulut.
   ESP8266 connects to a MQTT broker and executes lightning effects base on commands from the broker.
   Copyright 2018-2019 Aapo Rista / Vekotinverstas / Forum Virium Helsinki Oy
   MIT license

  NOTE
  You must install libraries below using Arduino IDE's 
  Sketch --> Include Library --> Manage Libraries... command

   PubSubClient (version >= 2.6.0 by Nick O'Leary)
   ArduinoJson (version > 5.13 < 6.0 by Benoit Blanchon)
   WiFiManager (version >= 0.14.0 by tzapu)
   
 **************************************************************************************/

#include "settings.h"             // Remember to copy settings-example.h to settings.h and check all values!
#include <Wire.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP8266WiFi.h>
#include <DNSServer.h>            // Local DNS Server used for redirecting all requests to the configuration portal
#include <ESP8266WebServer.h>     // Local WebServer used to serve the configuration portal
#include <WiFiManager.h>          // https://github.com/tzapu/WiFiManager WiFi Configuration Magic
#ifndef FastLED
  #include <FastLED.h>
#endif



// I2C settings
// #define SDA     D2
// #define SCL     D1

// Move to settings, perhaps?
CRGBPalette16 currentPalette;
TBlendType    currentBlending;



void callback(char* topic, byte* payload, unsigned int length) {
  Serial.print("Message arrived in topic: ");
  Serial.println(topic);
  Serial.print("Message:");
  for (int i = 0; i < length; i++) {
    Serial.print((char)payload[i]);
  }
  Serial.println();
  if (payload[0] == '0') {
    Serial.println("Protocol version was 0 as expected");
  }

  // There are several different palettes of colors demonstrated here.
  //
  // FastLED provides several 'preset' palettes: RainbowColors_p, RainbowStripeColors_p,
  // OceanColors_p, CloudColors_p, LavaColors_p, ForestColors_p, and PartyColors_p.
  switch (payload[1]) {
    case '0':
      Serial.println("Switch to PartyColors_p");
      currentPalette = PartyColors_p;
      break;
    case '1':
      Serial.println("Switch to OceanColors_p");
      currentPalette = OceanColors_p;
      break;
    case '2':
      Serial.println("Switch to LavaColors_p");
      currentPalette = LavaColors_p;
      break;
    case '3':
      Serial.println("Switch to ForestColors_p");
      currentPalette = ForestColors_p;
      break;
    case '4':
      Serial.println("Switch to PartyColors_p");
      currentPalette = PartyColors_p;
      break;
    default:
      // something
      break;
  }  
  Serial.println("-----------------------");
}

// Define and set up all variables / objects
WiFiClient wifiClient;
WiFiManager wifiManager;
PubSubClient client(MQTT_SERVER, 1883, callback, wifiClient);
String mac_str;
byte mac[6]; 
char macAddr[13];
unsigned long lastPing = 0;
char pingTopic[30];
char controlTopic[30];

/* Sensor variables */

void MqttSetup() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("No WiFi so no MQTT. Continuing.");
    return;
  }
  // Generate client name based on MAC address and last 8 bits of microsecond counter
  String clientName;
  clientName += "esp8266-";
  clientName += mac_str;
  clientName += "-";
  clientName += String(micros() & 0xff, 16);

  Serial.print("Connecting to ");
  Serial.print(MQTT_SERVER);
  Serial.print(" as ");
  Serial.println(clientName);
  client.setCallback(callback);

  if (client.connect((char*) clientName.c_str(), MQTT_USER, MQTT_PASSWORD)) {
    Serial.println("Connected to MQTT broker");
    Serial.print("Publish topic is: ");
    Serial.println(pingTopic);
    Serial.print("Subscribe topic is: ");
    Serial.println(controlTopic);
    SendPingToMQTT();
    client.subscribe(controlTopic);
  }
  else {
    Serial.println("MQTT connect failed");
    Serial.println("Will reset and try again...");
    delay(5000);
    // TODO: quit connecting after e.g. 20 seconds to enable standalone usage
    // abort();
  }
}

void setup() {
  mac_str = WiFi.macAddress();
  WiFi.macAddress(mac);
  sprintf(macAddr, "%2X%2X%2X%2X%2X%2X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  sprintf(pingTopic, "%s/%s", MQTT_PUB_TOPIC, macAddr);
  sprintf(controlTopic, "%s/%s", MQTT_SUB_TOPIC, macAddr);
  // Wire.begin(SDA, SCL);
  Serial.begin(115200);
  Serial.println();
  Serial.println();
  char ap_name[30];
  sprintf(ap_name, "%s_%s", AP_NAME, macAddr);
  Serial.print("AP name would be: ");
  Serial.println(ap_name);
  wifiManager.autoConnect(ap_name);
  MqttSetup();
  Serial.println("Init FastLED");
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection( TypicalLEDStrip );
  // FastLED.addLeds<LED_TYPE, LED_PIN, CLK_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection( TypicalLEDStrip );
  
  FastLED.setBrightness(  BRIGHTNESS );
  
  currentPalette = RainbowColors_p;
  currentBlending = LINEARBLEND;
  
}

void loop() {
  unsigned long now = millis();
  if (!client.loop()) {
    Serial.print("Client disconnected...");
    // TODO: increase reconnect from every loop() to every 60 sec or so
    MqttSetup();
    return;
  }
  if (lastPing + 10000 < now) {
    lastPing = now;
    SendPingToMQTT();
  }
//  runLedEffect();  
  static uint8_t startIndex = 0;
  startIndex = startIndex + 1; /* motion speed */

  FillLEDsFromPaletteColors( startIndex);
  
  FastLED.show();
  FastLED.delay(1000 / UPDATES_PER_SECOND);
  
}

void FillLEDsFromPaletteColors( uint8_t colorIndex)
{
    uint8_t brightness = BRIGHTNESS;
    
    for( int i = 0; i < NUM_LEDS; i++) {
        leds[i] = ColorFromPalette( currentPalette, colorIndex, brightness, currentBlending);
        colorIndex += 3;
    }
}

void runLedEffect() {
  // update led effect in this function
}

void SendPingToMQTT() {
  char cstr[16];
  // itoa(millis(), cstr, 10);
  sprintf(cstr, "%d,%d", NUM_LEDS, millis());
  Serial.print(pingTopic);
  Serial.print(" ");
  Serial.println(cstr);
  client.publish(pingTopic, cstr);
}