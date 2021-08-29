/**************************************************************************************
  Sketch to illuminate RGB LED strip using rain forecast
  Copyright 2020-2021 Aapo Rista
  MIT license
 **************************************************************************************/

#include <FS.h> // this needs to be first, or it all crashes and burns...
#include "settings.h" // Remember to copy settings-example.h to settings.h and check all the values!
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

// Define URL, lat and lon default values here, if there are different values in config.json, they are overwritten.
char http_url[150] = "http://weatherlamp.rista.net/yrweather.bin";
// Helsinki city centre
char latitude[16] = "60.172";
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

//flag for saving data
bool shouldSaveConfig = false;

//callback notifying us of the need to save config
void saveConfigCallback () {
  shouldSaveConfig = true;
  Serial.print("Should save config: ");
  Serial.println(shouldSaveConfig);
}

void requestData();
void requestData2();
void runLedEffect();
void setupSpiffs();
void saveConfig();


void setup()
{
  Serial.begin(115200);
  Serial.println();
  Serial.println();
  setupSpiffs();
  wifiManager.setSaveConfigCallback(saveConfigCallback);

  WiFiManagerParameter custom_http_url("http_url", "Data URL", http_url, 250);
  WiFiManagerParameter custom_latitude("latitude", "Latitude ° (60.172)", latitude, 16);
  WiFiManagerParameter custom_longitude("longitude", "Longitude ° (24.945)", longitude, 16);
  // Add all your parameters here
  wifiManager.addParameter(&custom_http_url);
  wifiManager.addParameter(&custom_latitude);
  wifiManager.addParameter(&custom_longitude);
  // Reset settings - wipe credentials for testing
  // wifiManager.resetSettings();
  mac_str = WiFi.macAddress();
  WiFi.macAddress(mac);

  sprintf(macAddr, "%2X%2X%2X%2X%2X%2X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  char ap_name[30];
  sprintf(ap_name, "%s_%s", AP_NAME, macAddr);
  Serial.print("AP name would be: ");
  Serial.println(ap_name);
  wifiManager.autoConnect(ap_name);

  // Wire.begin(SDA, SCL);
  // read updated parameters
  strcpy(http_url, custom_http_url.getValue());
  strcpy(latitude, custom_latitude.getValue());
  strcpy(longitude, custom_longitude.getValue());
  Serial.println(http_url);
  Serial.println(latitude);
  Serial.println(longitude);

  // Save the custom parameters to FS
  Serial.print("Setup Should save config: ");
  Serial.println(shouldSaveConfig);
  if (shouldSaveConfig) {
    saveConfig();
    shouldSaveConfig = false;
  }

  Serial.println("Init FastLED");
#ifdef CLKPIN
  FastLED.addLeds<LED_TYPE, DATAPIN, CLKPIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection( TypicalLEDStrip );
#else
  FastLED.addLeds<LED_TYPE, DATAPIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection(TypicalLEDStrip);
#endif

  FastLED.setBrightness(BRIGHTNESS);

  currentPalette = RainbowColors_p;
  currentBlending = LINEARBLEND;
}

void setupSpiffs() 
{
  //clean FS, for testing
  //Serial.println("Format FS...");
  //SPIFFS.format();

  // Read configuration from FS json
  Serial.println("Mounting FS...");

  if (SPIFFS.begin()) {
    Serial.println("mounted file system");
    if (SPIFFS.exists("/config.json")) {
      //file exists, reading and loading
      Serial.println("reading config file");
      File configFile = SPIFFS.open("/config.json", "r");
      if (configFile) {
        Serial.println("opened config file");
        size_t size = configFile.size();
        // Allocate a buffer to store contents of the file.
        std::unique_ptr<char[]> buf(new char[size]);
        configFile.readBytes(buf.get(), size);
        DynamicJsonDocument jsonBuffer(1024);
        auto error = deserializeJson(jsonBuffer, buf.get());
        serializeJson(jsonBuffer, Serial);
        if (!error) {
          Serial.println("\nParsed json");
          strcpy(http_url, jsonBuffer["http_url"]);
          strcpy(latitude, jsonBuffer["latitude"]);
          strcpy(longitude, jsonBuffer["longitude"]);
        } else {
          Serial.println("Failed to load json config");
        }
      }
    }
  } else {
    Serial.println("Failed to mount FS");
  }
}

void saveConfig() 
{
  Serial.println("Saving config");
  DynamicJsonDocument jsonBuffer(1024);
  jsonBuffer["http_url"] = http_url;
  jsonBuffer["latitude"] = latitude;
  jsonBuffer["longitude"] = longitude;
  File configFile = SPIFFS.open("/config.json", "w");
  if (!configFile) {
    Serial.println("failed to open config file for writing");
  }
  serializeJsonPretty(jsonBuffer, Serial);
  serializeJson(jsonBuffer, configFile);
  configFile.close();
}

void requestData()
{
  if (WiFi.status() == WL_CONNECTED)
  {
    HTTPClient http;
    http.addHeader("X-Client-Id", macAddr);
    http.setUserAgent(USER_AGENT);
    char serverPath[250];
    strcpy(serverPath, http_url);
    strcat(serverPath, "?lat=");
    strcat(serverPath, latitude);
    strcat(serverPath, "&lon=");
    strcat(serverPath, longitude);
    strcat(serverPath, "&client=");
    strcat(serverPath, macAddr);
    Serial.println(serverPath);

    // Your Domain name with URL path or IP address with path
    http.begin(serverPath);

    // Send HTTP GET request
    int httpResponseCode = http.GET();

    if (httpResponseCode > 0)
    {
      Serial.print("HTTP Response code: ");
      Serial.println(httpResponseCode);
      Serial.print("NUM_LEDS: ");
      Serial.println(NUM_LEDS);

      String payload = http.getString();
      Serial.println(payload);
      currentPalette = CRGBPalette16();

      for (int i = 0; i < 16; i++) {
        uint8_t i1 = i*4;
        uint8_t i2 = i*4 + 1;
        uint8_t i3 = i*4 + 2;
        // uint8_t wind = i*4 + 3;
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

void FillLEDsFromPaletteColors(uint8_t colorIndex)
{
  for (int i = 0; i < NUM_LEDS; i++)
  {
    leds[i] = ColorFromPalette(currentPalette, colorIndex, brightness, currentBlending);
    colorIndex += (int)255/NUM_LEDS;
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
