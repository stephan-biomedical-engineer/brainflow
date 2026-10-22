#include <string.h>

#include <gtest/gtest.h>

#include "iio_scan.h"


static IioScanElement element (const char *name, int index, const char *type)
{
    IioScanElement e;
    e.name = name;
    e.index = index;
    e.offset = 0;
    EXPECT_TRUE (parse_iio_scan_type (type, &e.type)) << type;
    return e;
}

static void put_le32 (uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; i++)
    {
        p[i] = (uint8_t)(v >> (8 * i));
    }
}

TEST (IioScanTest, ParsesTypes)
{
    IioScanType t;
    ASSERT_TRUE (parse_iio_scan_type ("le:s24/32>>0\n", &t));
    EXPECT_FALSE (t.big_endian);
    EXPECT_TRUE (t.is_signed);
    EXPECT_EQ (24, t.realbits);
    EXPECT_EQ (32, t.storagebits);
    EXPECT_EQ (0, t.shift);

    ASSERT_TRUE (parse_iio_scan_type ("be:u12/16>>4", &t));
    EXPECT_TRUE (t.big_endian);
    EXPECT_FALSE (t.is_signed);
    EXPECT_EQ (12, t.realbits);
    EXPECT_EQ (16, t.storagebits);
    EXPECT_EQ (4, t.shift);

    ASSERT_TRUE (parse_iio_scan_type ("le:s64/64>>0", &t));
    EXPECT_EQ (64, t.storagebits);
}

TEST (IioScanTest, RefusesBadTypes)
{
    IioScanType t;
    EXPECT_FALSE (parse_iio_scan_type ("", &t));
    EXPECT_FALSE (parse_iio_scan_type ("le:s12/16X2>>4", &t)); // repeat
    EXPECT_FALSE (parse_iio_scan_type ("xe:s24/32>>0", &t));
    EXPECT_FALSE (parse_iio_scan_type ("le:f24/32>>0", &t));
    EXPECT_FALSE (parse_iio_scan_type ("le:s24/24>>0", &t)); // storage not a power of two bytes
    EXPECT_FALSE (parse_iio_scan_type ("le:s24/32>>9", &t)); // does not fit
    EXPECT_FALSE (parse_iio_scan_type ("le:s24/32>>0 junk", &t));
    EXPECT_FALSE (parse_iio_scan_type ("le:s24/32>>0", NULL));
}

// ti-ads1299: eight voltages, the lead-off word, then the timestamp aligned to 8 bytes
TEST (IioScanTest, Ads1299LayoutAlignsTimestamp)
{
    std::vector<IioScanElement> e;
    e.push_back (element ("in_timestamp", 9, "le:s64/64>>0"));
    for (int i = 7; i >= 0; i--)
    {
        e.push_back (element ("in_voltage", i, "le:s24/32>>0"));
    }
    e.push_back (element ("in_voltage8_lead_off", 8, "le:u16/32>>0"));

    size_t size = compute_iio_layout (e);
    EXPECT_EQ (48u, size);
    for (int i = 0; i < 9; i++)
    {
        EXPECT_EQ (i, e[i].index);
        EXPECT_EQ ((size_t)(4 * i), e[i].offset);
    }
    EXPECT_EQ ("in_timestamp", e[9].name);
    EXPECT_EQ (40u, e[9].offset); // 36 rounded up to 8
}

// ti-ads1298 in mainline: eight voltages and nothing else
TEST (IioScanTest, Ads1298LayoutHasNoPadding)
{
    std::vector<IioScanElement> e;
    for (int i = 0; i < 8; i++)
    {
        e.push_back (element ("in_voltage", i, "le:s24/32>>0"));
    }
    EXPECT_EQ (32u, compute_iio_layout (e));
    EXPECT_EQ (28u, e[7].offset);
}

TEST (IioScanTest, EmptyLayout)
{
    std::vector<IioScanElement> e;
    EXPECT_EQ (0u, compute_iio_layout (e));
}

// The drivers push 24 bits zero-extended in 32; the sign comes from bit 23
TEST (IioScanTest, SignExtends24Bits)
{
    IioScanElement e = element ("in_voltage0", 0, "le:s24/32>>0");
    uint8_t scan[4];

    put_le32 (scan, 0x7fffff);
    EXPECT_EQ (8388607, decode_iio_element (scan, e));
    put_le32 (scan, 0x800000);
    EXPECT_EQ (-8388608, decode_iio_element (scan, e));
    put_le32 (scan, 0xffffff);
    EXPECT_EQ (-1, decode_iio_element (scan, e));
    put_le32 (scan, 0x000001);
    EXPECT_EQ (1, decode_iio_element (scan, e));
    // bits above realbits are ignored
    put_le32 (scan, 0xff000005);
    EXPECT_EQ (5, decode_iio_element (scan, e));
}

TEST (IioScanTest, DecodesUnsignedShiftedAndBigEndian)
{
    uint8_t scan[8];

    IioScanElement lead_off = element ("in_voltage8_lead_off", 8, "le:u16/32>>0");
    put_le32 (scan, 0x8001);
    EXPECT_EQ (0x8001, decode_iio_element (scan, lead_off));

    IioScanElement be = element ("x", 0, "be:s12/16>>4");
    scan[0] = 0x80; // 0x8000 >> 4 = 0x800, the most negative 12-bit value
    scan[1] = 0x00;
    EXPECT_EQ (-2048, decode_iio_element (scan, be));

    IioScanElement ts = element ("in_timestamp", 0, "le:s64/64>>0");
    int64_t ns = 1792637230123456789LL;
    for (int i = 0; i < 8; i++)
    {
        scan[i] = (uint8_t)((uint64_t)ns >> (8 * i));
    }
    EXPECT_EQ (ns, decode_iio_element (scan, ts));
}
