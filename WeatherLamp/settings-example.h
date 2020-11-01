// MQTT settings
#define MQTT_SERVER "mqtt.example.org"
#define MQTT_PORT 1883
#define MQTT_USER "mqtt_user_with_read-write_permission_to_topic"
#define MQTT_PASSWORD "mqtt_password"
#define MQTT_SUB_TOPIC  "led/control"
#define MQTT_PUB_TOPIC  "led/ping"

#define AP_NAME  "PikkuJoulu"
#define NUM_LEDS 6
#define NUM_LEDS    10
#ifndef FastLED
  #include <FastLED.h>
#endif
#define LED_PIN     D4
#define CLK_PIN     D5
#define BRIGHTNESS  150
#define LED_TYPE    WS2812B
// #define LED_TYPE    LPD8806
#define COLOR_ORDER GRB
CRGB leds[NUM_LEDS];
#define UPDATES_PER_SECOND 50
