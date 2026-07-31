#include <driver/i2s.h>

#define SAMPLE_RATE 16000
#define CHUNK       256

void setup() {
  Serial.begin(921600);

  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = CHUNK,
    .use_apll = false
  };
  i2s_pin_config_t pins = {
    .bck_io_num = 26,
    .ws_io_num = 25,
    .data_out_num = I2S_PIN_NO_CHANGE,   .data_in_num = 33
  };
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
}

int32_t raw[CHUNK];
int16_t pcm[CHUNK];

void loop() {
  size_t n = 0;
  i2s_read(I2S_NUM_0, raw, sizeof(raw), &n, portMAX_DELAY);
  int samples = n / 4;
  for (int i = 0; i < samples; i++) pcm[i] = raw[i] >> 14;

  Serial.write((uint8_t*)"\xAA\x55", 2);
  Serial.write((uint8_t)(samples & 0xFF));
  Serial.write((uint8_t*)pcm, samples * 2);
}
