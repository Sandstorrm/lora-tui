// Drives the firmware's real blp::Engine from stdin/stdout with a virtual clock, so the Python
// controller can be tested against the actual device code without any hardware.
//
//   begin <now>                      tick <now>
//   rx <now> <rssi> <snr> <hex>      txdone <now> <ok 0/1>
//   cli <hex>      bytes the "CLI" prints (device -> controller)      want <n>
//   state                            quit
// Replies (one line each): send <hex> | profile <n> | rxpush <hex> | state ... | ok
#include "lora_link_engine.h"
#include <deque>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using namespace blp;

static std::string toHex(const uint8_t *d, size_t n) {
    static const char *h = "0123456789abcdef";
    std::string s;
    for (size_t i = 0; i < n; i++) { s += h[d[i] >> 4]; s += h[d[i] & 15]; }
    return s;
}
static std::vector<uint8_t> fromHex(const std::string &s) {
    std::vector<uint8_t> v;
    for (size_t i = 0; i + 1 < s.size(); i += 2) v.push_back((uint8_t)std::stoi(s.substr(i, 2), nullptr, 16));
    return v;
}

struct H : Host {
    std::deque<uint8_t> out;
    size_t rxCap = 4096;
    void sendFrame(const uint8_t *f, size_t n) override { std::cout << "send " << std::string((const char *)f, n) << "\n"; }
    void setProfile(uint8_t i) override { std::cout << "profile " << (int)i << "\n"; }
    void setPower(uint8_t p) override { std::cout << "power " << (int)p << "\n"; }
    uint8_t defPower = 22;
    uint8_t defaultPower() override { return defPower; }
    size_t rxSpace() override { return rxCap; }
    void rxPush(const uint8_t *d, size_t n) override { std::cout << "rxpush " << toHex(d, n) << "\n"; }
    size_t txAvail() override { return out.size(); }
    size_t txPeek(uint8_t *dst, size_t max) override {
        size_t n = 0;
        for (auto it = out.begin(); it != out.end() && n < max; ++it) dst[n++] = *it;
        return n;
    }
    void txDrop(size_t n) override { while (n-- && !out.empty()) out.pop_front(); }
    void txClear() override { out.clear(); }
};

int main() {
    H host;
    Engine eng(host);
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "begin") { uint32_t t; in >> t; eng.begin(t); }
        else if (cmd == "tick") { uint32_t t; in >> t; eng.tick(t); }
        else if (cmd == "rx") {
            uint32_t t; int rssi, snr; std::string frame;
            in >> t >> rssi >> snr >> std::ws;
            std::getline(in, frame); // frames may contain spaces
            eng.onFrame(t, (const uint8_t *)frame.data(), frame.size(), rssi, snr);
        } else if (cmd == "txdone") { uint32_t t; int ok; in >> t >> ok; eng.onTxDone(t, ok != 0); }
        else if (cmd == "cli") { std::string hx; in >> hx; for (uint8_t b : fromHex(hx)) host.out.push_back(b); }
        else if (cmd == "want") { int w; in >> w; eng.setWant((uint8_t)w); }
        else if (cmd == "defpower") { int w; in >> w; host.defPower = (uint8_t)w; }
        else if (cmd == "state") {
            const Stats &s = eng.stats();
            std::cout << "state linked=" << (eng.state() == LinkState::Linked) << " profile=" << (int)eng.profile()
                      << " switching=" << eng.switching() << " rx=" << s.framesRx << " tx=" << s.framesTx
                      << " bad=" << s.badFrames << " dup=" << s.dupSegments << " retx=" << s.retransmits
                      << " in=" << s.bytesIn << " out=" << s.bytesOut << " sessions=" << s.sessions
                      << " power=" << (int)eng.power() << " reverts=" << s.revertsToHome << " queued=" << host.out.size() << "\n";
        } else if (cmd == "quit") { std::cout << "ok\n" << std::flush; break; }
        std::cout << "ok\n" << std::flush;
    }
    return 0;
}
