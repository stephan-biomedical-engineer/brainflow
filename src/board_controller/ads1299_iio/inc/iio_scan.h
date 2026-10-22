#pragma once

#include <stddef.h>
#include <stdint.h>

#include <string>
#include <vector>

// Layout of a Linux IIO buffer, as described by scan_elements/*_index and *_type.
// Pure parsing and decoding, no system calls, so it builds and is tested on every platform.

struct IioScanType
{
    bool big_endian;
    bool is_signed;
    int realbits;
    int storagebits;
    int shift;
};

struct IioScanElement
{
    std::string name; // e.g. "in_voltage0", "in_timestamp"
    int index;
    IioScanType type;
    size_t offset; // filled by compute_iio_layout
};

// Parses a *_type string such as "le:s24/32>>0". Repeated elements ("X2") are refused.
bool parse_iio_scan_type (const std::string &text, IioScanType *type);

// Sorts the elements by index and gives each its offset in a scan: every element is aligned to
// its own storage size, and the scan to its largest element. Returns the scan size in bytes, or
// 0 when there is nothing to lay out.
size_t compute_iio_layout (std::vector<IioScanElement> &elements);

// Reads one element from a scan: byte order, shift, then sign extension from realbits.
int64_t decode_iio_element (const uint8_t *scan, const IioScanElement &element);
