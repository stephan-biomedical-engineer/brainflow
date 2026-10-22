#include "ads1299_iio_board.h"

#ifdef __linux__

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <locale>
#include <sstream>
#include <system_error>

#include "timestamp.h"

#define ADS1299_IIO_SAMPLING_RATE 250
#define ADS1299_IIO_KFIFO_LENGTH 1024
#define ADS1299_IIO_DEFAULT_ROOT "/sys/bus/iio/devices"


// sysfs helpers: an attribute is one line of text

static bool read_attr (const std::string &path, std::string &value)
{
    int f = open (path.c_str (), O_RDONLY);
    if (f < 0)
    {
        return false;
    }
    char buf[256];
    ssize_t n = read (f, buf, sizeof (buf) - 1);
    close (f);
    if (n < 0)
    {
        return false;
    }
    buf[n] = '\0';
    value = buf;
    while (!value.empty () && (value.back () == '\n' || value.back () == ' '))
    {
        value.pop_back ();
    }
    return true;
}

// returns 0 or an errno
static int write_attr (const std::string &path, const std::string &value)
{
    int f = open (path.c_str (), O_WRONLY | O_TRUNC);
    if (f < 0)
    {
        return errno;
    }
    ssize_t n = write (f, value.c_str (), value.size ());
    int err = (n < 0) ? errno : 0;
    if (close (f) < 0 && err == 0)
    {
        err = errno;
    }
    return err;
}

static std::vector<std::string> list_dir (const std::string &path)
{
    std::vector<std::string> names;
    DIR *d = opendir (path.c_str ());
    if (d == NULL)
    {
        return names;
    }
    struct dirent *e;
    while ((e = readdir (d)) != NULL)
    {
        if (e->d_name[0] != '.')
        {
            names.push_back (e->d_name);
        }
    }
    closedir (d);
    std::sort (names.begin (), names.end ());
    return names;
}

static bool parse_double (const std::string &text, double *value)
{
    // sysfs always uses '.', whatever the process locale says
    std::istringstream in (text);
    in.imbue (std::locale::classic ());
    in >> *value;
    return !in.fail ();
}

static bool all_digits (const std::string &s)
{
    return !s.empty () &&
        std::all_of (s.begin (), s.end (), [] (char c) { return c >= '0' && c <= '9'; });
}

// a scan index or channel number: digits only, no exceptions
static bool parse_index (const std::string &s, int *value)
{
    if (!all_digits (s) || s.size () > 9)
    {
        return false;
    }
    int v = 0;
    for (char c : s)
    {
        v = v * 10 + (c - '0');
    }
    *value = v;
    return true;
}

static std::string basename_of (const std::string &path)
{
    std::string p = path;
    while (p.size () > 1 && p.back () == '/')
    {
        p.pop_back ();
    }
    size_t slash = p.find_last_of ('/');
    return (slash == std::string::npos) ? p : p.substr (slash + 1);
}


ADS1299IIOBoard::ADS1299IIOBoard (int board_id, struct BrainFlowInputParams params)
    : Board (board_id, params)
{
    initialized = false;
    keep_alive = false;
    fd = -1;
    scan_size = 0;
    lead_off_element = -1;
    timestamp_element = -1;
    num_rows = 0;
    package_num_row = 0;
    timestamp_row = 0;
    lead_off_row = 0;
}

ADS1299IIOBoard::~ADS1299IIOBoard ()
{
    skip_logs = true;
    release_session ();
}

int ADS1299IIOBoard::find_device ()
{
    if (!params.serial_port.empty ())
    {
        dev_dir = params.serial_port;
        while (dev_dir.size () > 1 && dev_dir.back () == '/')
        {
            dev_dir.pop_back ();
        }
        return (int)BrainFlowExitCodes::STATUS_OK;
    }

    // no path given: the only IIO device whose name starts with "ads129"
    std::vector<std::string> found;
    for (const auto &entry : list_dir (ADS1299_IIO_DEFAULT_ROOT))
    {
        std::string name;
        std::string dir = std::string (ADS1299_IIO_DEFAULT_ROOT) + "/" + entry;
        if (read_attr (dir + "/name", name) && name.compare (0, 6, "ads129") == 0)
        {
            safe_logger (spdlog::level::info, "found {} at {}", name, dir);
            found.push_back (dir);
        }
    }
    if (found.size () != 1)
    {
        safe_logger (spdlog::level::err,
            "{} ads129x IIO devices found; set serial_port to the one to use, e.g. "
            "/sys/bus/iio/devices/iio:device0",
            found.size ());
        return (int)BrainFlowExitCodes::UNABLE_TO_OPEN_PORT_ERROR;
    }
    dev_dir = found[0];
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::discover_scan_elements ()
{
    voltage_names.clear ();
    lead_off_name.clear ();
    timestamp_name.clear ();
    unused_names.clear ();

    std::vector<std::pair<int, std::string>> voltages;
    const std::string prefix = "in_voltage";
    const std::string suffix = "_en";
    for (const auto &file : list_dir (dev_dir + "/scan_elements"))
    {
        if (file.size () <= suffix.size () ||
            file.compare (file.size () - suffix.size (), suffix.size (), suffix) != 0)
        {
            continue;
        }
        std::string base = file.substr (0, file.size () - suffix.size ());
        int channel = 0;
        std::string rest =
            (base.compare (0, prefix.size (), prefix) == 0) ? base.substr (prefix.size ()) : "";

        if (base == "in_timestamp")
        {
            timestamp_name = base;
        }
        else if (parse_index (rest, &channel))
        {
            voltages.push_back (std::make_pair (channel, base));
        }
        else if (rest.size () > 9 && rest.compare (rest.size () - 9, 9, "_lead_off") == 0 &&
            all_digits (rest.substr (0, rest.size () - 9)))
        {
            lead_off_name = base;
        }
        else
        {
            unused_names.push_back (base);
        }
    }
    std::sort (voltages.begin (), voltages.end ());

    for (size_t i = 0; i < voltages.size (); i++)
    {
        if (i < eeg_rows.size ())
        {
            voltage_names.push_back (voltages[i].second);
        }
        else
        {
            unused_names.push_back (voltages[i].second);
        }
    }

    if (voltage_names.empty ())
    {
        safe_logger (spdlog::level::err, "{} has no buffered voltage channels", dev_dir);
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }
    if (voltage_names.size () < eeg_rows.size ())
    {
        safe_logger (spdlog::level::warn, "{} voltage channels; the other EEG rows stay at 0",
            voltage_names.size ());
    }
    if (voltages.size () > eeg_rows.size ())
    {
        safe_logger (spdlog::level::warn, "{} voltage channels; only the first {} are read",
            voltages.size (), eeg_rows.size ());
    }
    if (lead_off_name.empty ())
    {
        safe_logger (spdlog::level::info, "no lead-off word in the buffer; its row stays at -1");
    }
    if (timestamp_name.empty ())
    {
        safe_logger (spdlog::level::info,
            "no IIO timestamp in the buffer; timestamps come from the host clock at read time");
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::prepare_session ()
{
    if (initialized)
    {
        safe_logger (spdlog::level::info, "Session already prepared");
        return (int)BrainFlowExitCodes::STATUS_OK;
    }

    // the reading thread must not throw, so the rows are read here, once
    try
    {
        const json &d = board_descr.at ("default");
        num_rows = d.at ("num_rows").get<int> ();
        package_num_row = d.at ("package_num_channel").get<int> ();
        timestamp_row = d.at ("timestamp_channel").get<int> ();
        lead_off_row = d.at ("other_channels").at (0).get<int> ();
        eeg_rows = d.at ("eeg_channels").get<std::vector<int>> ();
    }
    catch (json::exception &e)
    {
        safe_logger (spdlog::level::err, "board descriptor: {}", e.what ());
        return (int)BrainFlowExitCodes::NO_SUCH_DATA_IN_JSON_ERROR;
    }

    int res = find_device ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }
    std::string name;
    if (!read_attr (dev_dir + "/name", name))
    {
        safe_logger (spdlog::level::err, "{} is not an IIO device: no name attribute", dev_dir);
        return (int)BrainFlowExitCodes::UNABLE_TO_OPEN_PORT_ERROR;
    }
    safe_logger (spdlog::level::info, "using {} ({})", dev_dir, name);

    std::string enabled;
    if (!read_attr (dev_dir + "/buffer/enable", enabled))
    {
        safe_logger (spdlog::level::err, "{} has no buffer", dev_dir);
        return (int)BrainFlowExitCodes::UNABLE_TO_OPEN_PORT_ERROR;
    }
    if (enabled != "0")
    {
        safe_logger (spdlog::level::err,
            "the IIO buffer of {} is already enabled; is another program reading it?", dev_dir);
        return (int)BrainFlowExitCodes::PORT_ALREADY_OPEN_ERROR;
    }

    res = discover_scan_elements ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }

    // the board descriptor says 250 SPS, so the device must run at 250 SPS
    std::string rate = std::to_string (ADS1299_IIO_SAMPLING_RATE);
    std::string current;
    if (!read_attr (dev_dir + "/sampling_frequency", current) || current != rate)
    {
        int err = write_attr (dev_dir + "/sampling_frequency", rate);
        if (err != 0 || !read_attr (dev_dir + "/sampling_frequency", current) || current != rate)
        {
            safe_logger (spdlog::level::err, "failed to set sampling_frequency to {}: {}", rate,
                (err != 0) ? strerror (err) : current.c_str ());
            return (int)BrainFlowExitCodes::SET_PORT_ERROR;
        }
    }

    // BrainFlow timestamps are Unix time; the IIO clock must be CLOCK_REALTIME
    std::string clock;
    if (!timestamp_name.empty () && read_attr (dev_dir + "/current_timestamp_clock", clock) &&
        clock != "realtime")
    {
        int err = write_attr (dev_dir + "/current_timestamp_clock", "realtime");
        if (err != 0)
        {
            safe_logger (
                spdlog::level::err, "failed to set current_timestamp_clock: {}", strerror (err));
            return (int)BrainFlowExitCodes::SET_PORT_ERROR;
        }
        safe_logger (spdlog::level::warn, "current_timestamp_clock was {}; set to realtime", clock);
    }

    chardev = params.file.empty () ? "/dev/" + basename_of (dev_dir) : params.file;
    initialized = true;
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::setup_scan ()
{
    elements.clear ();
    voltage_elements.clear ();
    uv_per_code.clear ();
    lead_off_element = -1;
    timestamp_element = -1;

    std::vector<std::string> used = voltage_names;
    if (!lead_off_name.empty ())
    {
        used.push_back (lead_off_name);
    }
    if (!timestamp_name.empty ())
    {
        used.push_back (timestamp_name);
    }

    std::string se = dev_dir + "/scan_elements/";
    for (const auto &base : unused_names)
    {
        int err = write_attr (se + base + "_en", "0");
        if (err != 0)
        {
            // only costs bandwidth: the layout comes from what is enabled
            safe_logger (spdlog::level::warn, "failed to disable {}: {}", base, strerror (err));
        }
    }
    for (const auto &base : used)
    {
        int err = write_attr (se + base + "_en", "1");
        std::string index;
        std::string type;
        IioScanElement e;
        if (err != 0)
        {
            safe_logger (spdlog::level::err, "failed to enable {}: {}", base, strerror (err));
            return (int)BrainFlowExitCodes::BOARD_WRITE_ERROR;
        }
        if (!read_attr (se + base + "_index", index) || !parse_index (index, &e.index) ||
            !read_attr (se + base + "_type", type) || !parse_iio_scan_type (type, &e.type))
        {
            safe_logger (spdlog::level::err, "unreadable index or type for {}", base);
            return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
        }
        e.name = base;
        e.offset = 0;
        elements.push_back (e);
    }
    scan_size = compute_iio_layout (elements);

    for (size_t i = 0; i < elements.size (); i++)
    {
        if (elements[i].name == lead_off_name)
        {
            lead_off_element = (int)i;
        }
        else if (elements[i].name == timestamp_name)
        {
            timestamp_element = (int)i;
        }
    }
    // the gain sets the scale, and the gain may have changed since the last stream
    for (const auto &base : voltage_names)
    {
        std::string text;
        double mv = 0.0;
        if (!read_attr (dev_dir + "/" + base + "_scale", text) &&
            !read_attr (dev_dir + "/in_voltage_scale", text))
        {
            safe_logger (spdlog::level::err, "no scale for {}", base);
            return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
        }
        if (!parse_double (text, &mv))
        {
            safe_logger (spdlog::level::err, "unreadable scale for {}: {}", base, text);
            return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
        }
        for (size_t i = 0; i < elements.size (); i++)
        {
            if (elements[i].name == base)
            {
                voltage_elements.push_back ((int)i);
            }
        }
        uv_per_code.push_back (mv * 1000.0); // IIO voltage scale is in mV
    }

    // the kfifo default holds two scans; give the reader some slack
    int err = write_attr (dev_dir + "/buffer/length", std::to_string (ADS1299_IIO_KFIFO_LENGTH));
    if (err != 0)
    {
        safe_logger (spdlog::level::warn, "failed to set buffer/length: {}", strerror (err));
    }
    safe_logger (
        spdlog::level::debug, "{} scan elements, {} bytes per scan", elements.size (), scan_size);
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::set_buffer_enable (bool enable)
{
    int err = write_attr (dev_dir + "/buffer/enable", enable ? "1" : "0");
    if (err != 0)
    {
        safe_logger (spdlog::level::err, "failed to {} the IIO buffer: {}",
            enable ? "enable" : "disable", strerror (err));
        return (int)BrainFlowExitCodes::BOARD_WRITE_ERROR;
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::start_stream (int buffer_size, const char *streamer_params)
{
    if (!initialized)
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }
    if (keep_alive)
    {
        safe_logger (spdlog::level::err, "Streaming thread already running");
        return (int)BrainFlowExitCodes::STREAM_ALREADY_RUN_ERROR;
    }
    int res = prepare_for_acquisition (buffer_size, streamer_params);
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }
    res = setup_scan ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }
    res = set_buffer_enable (true);
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }
    fd = open (chardev.c_str (), O_RDONLY | O_NONBLOCK);
    if (fd < 0)
    {
        safe_logger (spdlog::level::err, "failed to open {}: {}", chardev, strerror (errno));
        set_buffer_enable (false);
        return (int)BrainFlowExitCodes::UNABLE_TO_OPEN_PORT_ERROR;
    }

    keep_alive = true;
    try
    {
        streaming_thread = std::thread ([this] { this->read_thread (); });
    }
    catch (const std::system_error &e)
    {
        safe_logger (spdlog::level::err, "failed to start the reading thread: {}", e.what ());
        keep_alive = false;
        close (fd);
        fd = -1;
        set_buffer_enable (false);
        return (int)BrainFlowExitCodes::STREAM_THREAD_ERROR;
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::stop_stream ()
{
    if (!keep_alive)
    {
        return (int)BrainFlowExitCodes::STREAM_THREAD_IS_NOT_RUNNING;
    }
    keep_alive = false;
    if (streaming_thread.joinable ())
    {
        streaming_thread.join ();
    }
    close (fd);
    fd = -1;

    std::string lost;
    if (read_attr (dev_dir + "/lost_samples", lost))
    {
        safe_logger ((lost == "0") ? spdlog::level::info : spdlog::level::warn,
            "the driver reports {} lost samples in this stream", lost);
    }
    return set_buffer_enable (false);
}

int ADS1299IIOBoard::release_session ()
{
    if (initialized)
    {
        if (keep_alive)
        {
            stop_stream ();
        }
        free_packages ();
        initialized = false;
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int ADS1299IIOBoard::config_board (std::string config, std::string &response)
{
    if (!initialized)
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }

    // "attribute" reads it, "attribute=value" writes it, both in the device directory
    size_t eq = config.find ('=');
    std::string name = config.substr (0, eq);
    bool is_write = (eq != std::string::npos);
    bool valid = !name.empty () &&
        std::all_of (name.begin (), name.end (),
            [] (char c) { return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_'; });
    std::string path = dev_dir + "/" + name;
    struct stat st;
    if (!valid || stat (path.c_str (), &st) != 0 || !S_ISREG (st.st_mode))
    {
        safe_logger (spdlog::level::err, "no attribute '{}' in {}", name, dev_dir);
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    if (is_write)
    {
        if (name == "sampling_frequency")
        {
            safe_logger (spdlog::level::err,
                "the sampling rate is fixed at {} SPS, as the board descriptor says",
                ADS1299_IIO_SAMPLING_RATE);
            return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
        }
        if (keep_alive)
        {
            safe_logger (spdlog::level::err, "stop the stream before changing {}", name);
            return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
        }
        int err = write_attr (path, config.substr (eq + 1));
        if (err != 0)
        {
            safe_logger (spdlog::level::err, "failed to write {}: {}", name, strerror (err));
            response = strerror (err);
            return (int)BrainFlowExitCodes::BOARD_WRITE_ERROR;
        }
    }
    if (!read_attr (path, response))
    {
        safe_logger (spdlog::level::err, "failed to read {}: {}", name, strerror (errno));
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

void ADS1299IIOBoard::read_thread ()
{
    std::vector<double> package (num_rows, 0.0);
    std::vector<uint8_t> buf (scan_size * 64);
    size_t fill = 0;
    double counter = 0;
    bool warned_eof = false;

    while (keep_alive)
    {
        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int res = poll (&pfd, 1, 1000);
        if (res < 0)
        {
            if (errno != EINTR)
            {
                safe_logger (spdlog::level::warn, "poll failed: {}", strerror (errno));
                std::this_thread::sleep_for (std::chrono::milliseconds (10));
            }
            continue;
        }
        if (res == 0)
        {
            safe_logger (spdlog::level::trace, "no data in 1 s");
            continue;
        }

        ssize_t n = read (fd, buf.data () + fill, buf.size () - fill);
        if (n < 0)
        {
            if (errno == ENODEV)
            {
                // the driver went away (module unloaded, device unbound)
                safe_logger (spdlog::level::err, "{} is gone; no more data", chardev);
                break;
            }
            if (errno != EAGAIN && errno != EINTR)
            {
                safe_logger (spdlog::level::warn, "read failed: {}", strerror (errno));
                std::this_thread::sleep_for (std::chrono::milliseconds (10));
            }
            continue;
        }
        if (n == 0)
        {
            // an IIO device never reports end of file; a FIFO without a writer does
            if (!warned_eof)
            {
                safe_logger (spdlog::level::warn, "{} reported end of file", chardev);
                warned_eof = true;
            }
            std::this_thread::sleep_for (std::chrono::milliseconds (10));
            continue;
        }
        double now = get_timestamp ();
        fill += (size_t)n;
        size_t scans = fill / scan_size;

        for (size_t s = 0; s < scans; s++)
        {
            const uint8_t *scan = buf.data () + s * scan_size;
            package[package_num_row] = counter;
            counter += 1;
            for (size_t i = 0; i < voltage_elements.size (); i++)
            {
                const IioScanElement &e = elements[voltage_elements[i]];
                package[eeg_rows[i]] = (double)decode_iio_element (scan, e) * uv_per_code[i];
            }
            package[lead_off_row] = (lead_off_element >= 0) ?
                (double)decode_iio_element (scan, elements[lead_off_element]) :
                -1.0;
            if (timestamp_element >= 0)
            {
                package[timestamp_row] =
                    (double)decode_iio_element (scan, elements[timestamp_element]) / 1e9;
            }
            else
            {
                // the last scan of the read is the newest
                package[timestamp_row] =
                    now - (double)(scans - 1 - s) / (double)ADS1299_IIO_SAMPLING_RATE;
            }
            push_package (package.data ());
        }

        size_t used = scans * scan_size;
        if (used > 0 && fill > used)
        {
            memmove (buf.data (), buf.data () + used, fill - used);
        }
        fill -= used;
    }
}

#else

ADS1299IIOBoard::ADS1299IIOBoard (int board_id, struct BrainFlowInputParams params)
    : Board (board_id, params)
{
}

ADS1299IIOBoard::~ADS1299IIOBoard ()
{
}

int ADS1299IIOBoard::prepare_session ()
{
    safe_logger (spdlog::level::err, "This board reads a Linux IIO device and runs only on Linux");
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int ADS1299IIOBoard::start_stream (int buffer_size, const char *streamer_params)
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int ADS1299IIOBoard::stop_stream ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int ADS1299IIOBoard::release_session ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int ADS1299IIOBoard::config_board (std::string config, std::string &response)
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

#endif
