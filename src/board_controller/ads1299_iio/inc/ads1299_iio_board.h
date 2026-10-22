#pragma once

#include <atomic>
#include <string>
#include <thread>
#include <vector>

#include "board.h"
#include "board_controller.h"
#include "iio_scan.h"

// TI ADS1299 through its Linux IIO driver. The kernel driver owns the chip; this board only
// enables the IIO buffer and reads it. It relies on the generic IIO ABI, so it works with the
// out-of-tree ti-ads1299 driver (lead-off word and timestamp in every scan) and with mainline's
// ti-ads1298 (neither).
class ADS1299IIOBoard : public Board
{
protected:
#ifdef __linux__
    bool initialized;
    std::atomic<bool> keep_alive;
    std::thread streaming_thread;
    int fd;

    std::string dev_dir; // /sys/bus/iio/devices/iio:deviceN
    std::string chardev; // /dev/iio:deviceN

    // scan elements, by their base name ("in_voltage0", "in_voltage8_lead_off", "in_timestamp")
    std::vector<std::string> voltage_names;
    std::string lead_off_name;
    std::string timestamp_name;
    std::vector<std::string> unused_names;

    std::vector<IioScanElement> elements;
    size_t scan_size;
    std::vector<int> voltage_elements; // index into elements, per EEG row
    int lead_off_element;
    int timestamp_element;
    std::vector<double> uv_per_code;

    int find_device ();
    int discover_scan_elements ();
    int setup_scan ();
    int set_buffer_enable (bool enable);
    void read_thread ();
#endif

public:
    ADS1299IIOBoard (int board_id, struct BrainFlowInputParams params);
    ~ADS1299IIOBoard () override;

    int prepare_session () override;
    int start_stream (int buffer_size, const char *streamer_params) override;
    int stop_stream () override;
    int release_session () override;
    int config_board (std::string config, std::string &response) override;
};
