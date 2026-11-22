// Scratch probe (COP-1 investigation): run F' v4.3.0's own CcsdsTcFrameDetector, as built for DoomSat,
// against TC frames of each COP-1 type, then mirror FrameAccumulator::processRing's scan loop over a
// stream holding an AD frame, a BC frame and a BD frame back to back.
#include <cstdio>
#include <cstring>
#include <vector>
#include <Svc/FrameAccumulator/FrameDetector/CcsdsTcFrameDetector.hpp>
#include <Svc/Ccsds/Utils/CRC16.hpp>
#include <Utils/Types/CircularBuffer.hpp>
#include "config/FppConstantsAc.hpp"

using Svc::FrameDetector;

// Build a TC frame: 5-byte primary header, data field, 2-byte FECF (CRC16, as TcDeframer checks it).
static std::vector<U8> tcFrame(bool bypass, bool ctrl, U16 scid, U8 vc, U8 seq, const std::vector<U8>& data) {
    const U16 len = static_cast<U16>(5 + data.size() + 2);
    U16 w0 = static_cast<U16>((bypass ? 0x2000 : 0) | (ctrl ? 0x1000 : 0) | (scid & 0x3FF));
    U16 w1 = static_cast<U16>(((vc & 0x3F) << 10) | ((len - 1) & 0x3FF));
    std::vector<U8> f = {U8(w0 >> 8), U8(w0), U8(w1 >> 8), U8(w1), seq};
    f.insert(f.end(), data.begin(), data.end());
    U16 crc = Svc::Ccsds::Utils::CRC16::compute(f.data(), static_cast<FwSizeType>(f.size()));
    f.push_back(U8(crc >> 8));
    f.push_back(U8(crc));
    return f;
}

static const char* name(FrameDetector::Status s) {
    switch (s) {
        case FrameDetector::FRAME_DETECTED: return "FRAME_DETECTED";
        case FrameDetector::NO_FRAME_DETECTED: return "NO_FRAME_DETECTED";
        case FrameDetector::MORE_DATA_NEEDED: return "MORE_DATA_NEEDED";
    }
    return "?";
}

int main() {
    const U16 scid = ComCfg::SpacecraftId;
    // A space packet stand-in for a command: 1800 C000 0005 + 6 bytes
    const std::vector<U8> sp = {0x18, 0x00, 0xC0, 0x00, 0x00, 0x05, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04};
    struct Case { const char* label; std::vector<U8> frame; };
    std::vector<Case> cases = {
        {"BD  (bypass=1 ctrl=0) N(S)=0, VC1", tcFrame(true, false, scid, 1, 0, sp)},
        {"BD  (bypass=1 ctrl=0) N(S)=200, VC2", tcFrame(true, false, scid, 2, 200, sp)},
        {"AD  (bypass=0 ctrl=0) N(S)=5, VC1", tcFrame(false, false, scid, 1, 5, sp)},
        {"BC  Unlock (bypass=1 ctrl=1) data=00", tcFrame(true, true, scid, 1, 0, {0x00})},
        {"BC  Set V(R)=7 (bypass=1 ctrl=1) data=82 00 07", tcFrame(true, true, scid, 1, 0, {0x82, 0x00, 0x07})},
        {"AC  (bypass=0 ctrl=1, illegal)", tcFrame(false, true, scid, 1, 0, {0x00})},
    };
    Svc::FrameDetectors::CcsdsTcFrameDetector detector;
    std::printf("SpacecraftId=0x%04X; detector expects first U16 == 0x%04X\n", scid,
                static_cast<unsigned>((0x1 << 13) | scid));
    for (auto& c : cases) {
        U8 store[2048];
        Types::CircularBuffer ring(store, sizeof store);
        ring.serialize(c.frame.data(), static_cast<FwSizeType>(c.frame.size()));
        FwSizeType size_out = 0;
        FrameDetector::Status st = detector.detect(ring, size_out);
        std::printf("%-48s first U16=%02X%02X len=%3zu -> %s size_out=%lu\n", c.label, c.frame[0], c.frame[1],
                    c.frame.size(), name(st), static_cast<unsigned long>(size_out));
    }

    // Mirror FrameAccumulator::processRing (FrameAccumulator.cpp:102-186): detect; on FRAME_DETECTED consume
    // size_out; on NO_FRAME_DETECTED rotate one byte (no event); on MORE_DATA_NEEDED stop.
    std::vector<U8> stream;
    for (int i : {2, 3, 0}) stream.insert(stream.end(), cases[i].frame.begin(), cases[i].frame.end());
    U8 store[2048];
    Types::CircularBuffer ring(store, sizeof store);
    ring.serialize(stream.data(), static_cast<FwSizeType>(stream.size()));
    unsigned detected = 0, discardedBytes = 0;
    for (FwSizeType i = 0; i < ring.get_capacity() && ring.get_allocated_size() > 0; i++) {
        FwSizeType size_out = 0;
        FrameDetector::Status st = detector.detect(ring, size_out);
        if (st == FrameDetector::FRAME_DETECTED) {
            detected++;
            std::printf("stream: frame of %lu bytes detected after %u discarded bytes\n",
                        static_cast<unsigned long>(size_out), discardedBytes);
            (void)ring.rotate(size_out);
        } else if (st == FrameDetector::MORE_DATA_NEEDED) {
            break;
        } else {
            (void)ring.rotate(1);
            discardedBytes++;
        }
    }
    std::printf("stream of AD(%zu B) + BC(%zu B) + BD(%zu B): %u frame(s) detected, %u byte(s) discarded one at a time\n",
                cases[2].frame.size(), cases[3].frame.size(), cases[0].frame.size(), detected, discardedBytes);
    return 0;
}
