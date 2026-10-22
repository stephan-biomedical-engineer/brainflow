#include "iio_scan.h"

#include <algorithm>
#include <stdio.h>


bool parse_iio_scan_type (const std::string &text, IioScanType *type)
{
    char endian[3] = {0};
    char sign = 0;
    int realbits = 0;
    int storagebits = 0;
    int shift = 0;
    int consumed = 0;

    if (type == NULL)
    {
        return false;
    }
    // "le:s24/32>>0"; a repeat ("le:s12/16X2>>4") stops the match at 'X'
    if (sscanf (text.c_str (), "%2[bel]:%c%d/%d>>%d%n", endian, &sign, &realbits, &storagebits,
            &shift, &consumed) != 5)
    {
        return false;
    }
    // only trailing whitespace (the newline of the sysfs file) may follow
    for (size_t i = (size_t)consumed; i < text.size (); i++)
    {
        if (text[i] != '\n' && text[i] != ' ' && text[i] != '\r' && text[i] != '\t')
        {
            return false;
        }
    }
    std::string e (endian);
    if ((e != "le" && e != "be") || (sign != 's' && sign != 'u'))
    {
        return false;
    }
    if (storagebits != 8 && storagebits != 16 && storagebits != 32 && storagebits != 64)
    {
        return false;
    }
    if (realbits < 1 || shift < 0 || realbits + shift > storagebits)
    {
        return false;
    }
    type->big_endian = (e == "be");
    type->is_signed = (sign == 's');
    type->realbits = realbits;
    type->storagebits = storagebits;
    type->shift = shift;
    return true;
}

size_t compute_iio_layout (std::vector<IioScanElement> &elements)
{
    std::sort (elements.begin (), elements.end (),
        [] (const IioScanElement &a, const IioScanElement &b) { return a.index < b.index; });

    size_t offset = 0;
    size_t largest = 0;
    for (auto &element : elements)
    {
        size_t bytes = (size_t)element.type.storagebits / 8;
        offset = (offset + bytes - 1) / bytes * bytes;
        element.offset = offset;
        offset += bytes;
        largest = std::max (largest, bytes);
    }
    if (largest == 0)
    {
        return 0;
    }
    return (offset + largest - 1) / largest * largest;
}

int64_t decode_iio_element (const uint8_t *scan, const IioScanElement &element)
{
    const IioScanType &t = element.type;
    int bytes = t.storagebits / 8;
    const uint8_t *p = scan + element.offset;
    uint64_t raw = 0;

    for (int i = 0; i < bytes; i++)
    {
        int byte = t.big_endian ? i : bytes - 1 - i;
        raw = (raw << 8) | p[byte];
    }
    raw >>= t.shift;
    if (t.realbits < 64)
    {
        uint64_t mask = (((uint64_t)1) << t.realbits) - 1;
        raw &= mask;
        if (t.is_signed && (raw & (((uint64_t)1) << (t.realbits - 1))))
        {
            raw |= ~mask;
        }
    }
    return (int64_t)raw;
}
