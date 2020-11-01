/**************************************************************************************
   Sketch to illuminat RGB LED strip using weather forecast
   Copyright 2020 Aapo Rista
   MIT license

  NOTE
  You must install libraries below using Arduino IDE's
  Sketch --> Include Library --> Manage Libraries... command

   PubSubClient (version >= 2.6.0 by Nick O'Leary)
   ArduinoJson (version > 5.13 < 6.0 by Benoit Blanchon)
   WiFiManager (version >= 0.14.0 by tzapu)

 **************************************************************************************/

#include "settings.h" // Remember to copy settings-example.h to settings.h and check all values!
#include <Wire.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP8266WiFi.h>
#include <DNSServer.h> // Local DNS Server used for redirecting all requests to the configuration portal
#include <ESP8266mDNS.h>
#include <ESP8266WebServer.h> // Local WebServer used to serve the configuration portal
#include <WiFiManager.h>      // https://github.com/tzapu/WiFiManager WiFi Configuration Magic
#include <ESP8266HTTPClient.h>
#ifndef FastLED
#include <FastLED.h>
#endif

// I2C settings
// #define SDA     D2
// #define SCL     D1

// define your default values here, if there are different values in config.json, they are overwritten.
char http_url[250] = "http://rista.net/?weatherlamp=true";
char latitude[16] = "60.123";
char longitude[16] = "24.945";

// Move to settings, perhaps?
CRGBPalette16 currentPalette;
TBlendType currentBlending;
uint8_t currentMode = '0';
uint8_t activeEffect = '0';
uint8_t brightness = BRIGHTNESS;
static uint8_t startIndex = 0;
uint8_t colorIndex = 0;

uint8_t r = 0;
uint8_t g = 0;
uint8_t b = 0;

// Define and set up all variables / objects
WiFiClient wifiClient;
WiFiManager wifiManager;
String mac_str;
byte mac[6];
char macAddr[13];
unsigned long lastPing = 0;
char pingTopic[50];
char controlTopic[50];
char controlTopicBrc[50];

/* Sensor variables */

void requestData();
void requestData2();
void runLedEffect();

void setup()
{
  WiFiManagerParameter custom_http_url("server", "Data URL", http_url, 250);
  WiFiManagerParameter custom_latitude("port", "Latitude ° (60.172)", "60.172", 16);
  WiFiManagerParameter custom_longitude("user", "Longitude ° (24.945)", longitude, 16);
  // Add all your parameters here
  wifiManager.addParameter(&custom_http_url);
  wifiManager.addParameter(&custom_latitude);
  wifiManager.addParameter(&custom_longitude);
  // wifiManager.resetSettings();
  mac_str = WiFi.macAddress();
  WiFi.macAddress(mac);
  // Wire.begin(SDA, SCL);
  Serial.begin(115200);
  Serial.println();
  Serial.println();
  //read updated parameters
  strcpy(http_url, custom_http_url.getValue());
  strcpy(latitude, custom_latitude.getValue());
  strcpy(longitude, custom_longitude.getValue());
  Serial.println(http_url);
  Serial.println(latitude);
  Serial.println(longitude);
  // Serial.println(mqtt_password);
  // Serial.println(room_token);
  sprintf(macAddr, "%2X%2X%2X%2X%2X%2X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  char ap_name[30];
  sprintf(ap_name, "%s_%s", AP_NAME, macAddr);
  Serial.print("AP name would be: ");
  Serial.println(ap_name);
  wifiManager.autoConnect(ap_name);
  Serial.println("Init FastLED");
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection(TypicalLEDStrip);
  // FastLED.addLeds<LED_TYPE, LED_PIN, CLK_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection( TypicalLEDStrip );

  FastLED.setBrightness(BRIGHTNESS);

  currentPalette = RainbowColors_p;
  currentBlending = LINEARBLEND;
}

void requestData()
{
  if (WiFi.status() == WL_CONNECTED)
  {
    HTTPClient http;
    String http_url = "http://porr.rista.fi/weatherlamp.bin";
    String serverPath = http_url + "?temperature=24.37";

    // Your Domain name with URL path or IP address with path
    http.begin(serverPath.c_str());

    // Send HTTP GET request
    int httpResponseCode = http.GET();

    if (httpResponseCode > 0)
    {
      Serial.print("HTTP Response code: ");
      Serial.println(httpResponseCode);
      String payload = http.getString();
      Serial.println(payload);
      currentPalette = CRGBPalette16();

      for (int i = 0; i < 16; i++) {
        uint8_t i1 = i*3;
        uint8_t i2 = i*3 + 1;
        uint8_t i3 = i*3 + 2;
        uint8_t r = (uint8_t)payload[i1];
        uint8_t g = (uint8_t)payload[i2];
        uint8_t b = (uint8_t)payload[i3];
        Serial.print(r);
        Serial.print(",");
        Serial.print(g);
        Serial.print(",");
        Serial.print(b);
        Serial.println();
        currentPalette[i] = CRGB(r, g, b);
      }
    }
    else
    {
      Serial.print("Error code: ");
      Serial.println(httpResponseCode);
    }
    // Free resources
    http.end();
  }
  else
  {
    Serial.println("WiFi Disconnected");
  }
}

void loop()
{
  unsigned long now = millis();
  if (lastPing + 10000 < now)
  {
    requestData();
    lastPing = now;
  }
  runLedEffect();
  FastLED.show();
  FastLED.delay(1000 / UPDATES_PER_SECOND);
}

/**
   Mode is switched always when a valid MQTT message is received
*/
void switchMode(byte *payload, unsigned int length)
{
  colorIndex = 0;
  // There are several different palettes of colors demonstrated here.
  //
  // FastLED provides several 'preset' palettes: RainbowColors_p, RainbowStripeColors_p,
  // OceanColors_p, CloudColors_p, LavaColors_p, ForestColors_p, and PartyColors_p.
  switch (payload[2])
  {
  case '0':
    Serial.println("Switch to RainbowColors_p");
    currentPalette = RainbowColors_p;
    break;
  case '1':
    Serial.println("Switch to RainbowStripeColors_p");
    currentPalette = RainbowStripeColors_p;
    break;
  case '2':
    Serial.println("Switch to OceanColors_p");
    currentPalette = OceanColors_p;
    break;
  case '3':
    Serial.println("Switch to CloudColors_p");
    currentPalette = CloudColors_p;
    break;
  case '4':
    Serial.println("Switch to LavaColors_p");
    currentPalette = LavaColors_p;
    break;
  case '5':
    Serial.println("Switch to ForestColors_p");
    currentPalette = ForestColors_p;
    break;
  case '6':
    Serial.println("Switch to PartyColors_p");
    currentPalette = PartyColors_p;
    break;
  default:
    Serial.print("Invalid palette: ");
    Serial.println(payload[2]);
    break;
  }
}

/**
   Mode is switched always when a valid MQTT message is received
*/
void setSolidColor(byte *payload, unsigned int length)
{
  r = payload[2];
  g = payload[3];
  b = payload[4];
  Serial.println(r);
  Serial.println(g);
  Serial.println(b);
}

void setActiveEffect(byte *payload, unsigned int length)
{
  if ((payload[2] >= '0') && (payload[2] <= '2'))
  {
    activeEffect = payload[2];
    Serial.println("activeEffect set");
  }
  else
  {
    Serial.print("Invalid effect: ");
    Serial.println(payload[2]);
  }
}

void FillLEDsFromPaletteColors(uint8_t colorIndex)
{
  for (int i = 0; i < NUM_LEDS; i++)
  {
    leds[i] = ColorFromPalette(currentPalette, colorIndex, brightness, currentBlending);
    colorIndex += 8;
  }
}

void FillLEDsWithSolidColor()
{
  for (int i = 0; i < NUM_LEDS; i++)
  {
    leds[i].setRGB(r, g, b);
  }
}


void runLedEffect()
{
  // Serial.println(currentMode);
  switch (currentMode)
  {
  case '0':
    static uint8_t startIndex = 0;
    // startIndex = startIndex + 1; /* motion speed */
    FillLEDsFromPaletteColors(startIndex);
    break;
  case '1':
    FillLEDsWithSolidColor();
    break;
  default:
    Serial.println("Got invalid mode (in runLedEffect)");
    break;
  }
}
