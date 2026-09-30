
#include <cctype>
#include <string>
#include <vector>

#include <SDL2/SDL.h>
#include <proto-include.h>
#include <orbis/AppInstUtil.h>
#include <orbis/Pad.h>
#include <orbis/Sysmodule.h>
#include <orbis/UserService.h>

#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

static_assert(sizeof(unsigned long) == 8, "PS4 BGFT ABI requires 64-bit unsigned long");

struct TtgBgftInitParams {
    void* heap;
    size_t heapSize;
};

struct TtgBgftDownloadParam {
    int32_t userId;
    int32_t entitlementType;
    const char* id;
    const char* contentUrl;
    const char* contentExUrl;
    const char* contentName;
    const char* iconPath;
    const char* skuId;
    uint32_t option;
    const char* playgoScenarioId;
    const char* releaseDate;
    const char* packageType;
    const char* packageSubType;
    unsigned long packageSize;
};

struct TtgBgftTaskProgress {
    uint32_t bits;
    int32_t errorResult;
    unsigned long length;
    unsigned long transferred;
    unsigned long lengthTotal;
    unsigned long transferredTotal;
    uint32_t numIndex;
    uint32_t numTotal;
    uint32_t restSec;
    uint32_t restSecTotal;
    int32_t preparingPercent;
    int32_t localCopyPercent;
};

extern "C" {
int32_t sceBgftServiceIntInit(TtgBgftInitParams* params);
int32_t sceBgftServiceIntTerm();
int32_t sceBgftServiceDownloadStartTask(int32_t taskId);
int32_t sceBgftServiceDownloadPauseTask(int32_t taskId);
int32_t sceBgftServiceDownloadResumeTask(int32_t taskId);
int32_t sceBgftServiceDownloadStopTask(int32_t taskId);
int32_t sceBgftServiceDownloadGetProgress(int32_t taskId, TtgBgftTaskProgress* progress);
int32_t sceBgftServiceIntDebugDownloadRegisterPkg(TtgBgftDownloadParam* params, int32_t* taskId);
}

static constexpr uint32_t kBgftDisableCdnQueryParam = 0x10000u;

static constexpr int kPort = 49560;
static constexpr const char* kVersion = "1.0.0";
static constexpr const char* kTitleId = "TTGI00001";
static constexpr size_t kBgftHeapSize = 1024 * 1024;
static constexpr int kFrameW = 1920;
static constexpr int kFrameH = 1080;

enum class QueueState {
    Queued,
    Starting,
    Active,
    Paused,
    Completed,
    Cancelled,
    Error
};

struct QueueItem {
    uint64_t id = 0;
    std::string name;
    std::string url;
    uint64_t size = 0;
    int task_id = -1;
    QueueState state = QueueState::Queued;
    uint64_t transferred = 0;
    uint64_t total = 0;
    int percent = 0;
    int error = 0;
};

struct GameInfo {
    std::string title_id;
    std::string title;
    std::string version;
    std::string category;
};

struct PairState {
    std::string request_id;
    std::string client;
    std::string token;
    bool waiting = false;
    bool approved = false;
    bool rejected = false;
};

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static std::vector<QueueItem> g_queue;
static std::vector<GameInfo> g_games;
static PairState g_pair;
static uint64_t g_next_id = 1;
static volatile bool g_running = true;
static void* g_bgft_heap = nullptr;
static bool g_bgft_ready = false;
static int g_user_id = -1;

static std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    snprintf(b, sizeof(b), "\\u%04x", c);
                    out += b;
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    return out;
}

static std::string state_name(QueueState s) {
    switch (s) {
        case QueueState::Queued: return "queued";
        case QueueState::Starting: return "starting";
        case QueueState::Active: return "active";
        case QueueState::Paused: return "paused";
        case QueueState::Completed: return "completed";
        case QueueState::Cancelled: return "cancelled";
        case QueueState::Error: return "error";
    }
    return "unknown";
}

static bool read_file(const std::string& path, std::vector<uint8_t>& out) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) return false;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return false; }
    long n = ftell(f);
    if (n <= 0) { fclose(f); return false; }
    rewind(f);
    out.resize(static_cast<size_t>(n));
    size_t got = fread(out.data(), 1, out.size(), f);
    fclose(f);
    return got == out.size();
}

#pragma pack(push, 1)
struct SfoHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t key_table_offset;
    uint32_t data_table_offset;
    uint32_t entries_count;
};
struct SfoEntry {
    uint16_t key_offset;
    uint16_t format;
    uint32_t length;
    uint32_t max_length;
    uint32_t data_offset;
};
#pragma pack(pop)

static std::string sfo_get(const std::vector<uint8_t>& b, const char* wanted) {
    if (b.size() < sizeof(SfoHeader)) return {};
    const auto* h = reinterpret_cast<const SfoHeader*>(b.data());
    if (h->entries_count > 4096) return {};
    size_t table = sizeof(SfoHeader);
    if (table + h->entries_count * sizeof(SfoEntry) > b.size()) return {};
    for (uint32_t i = 0; i < h->entries_count; ++i) {
        const auto* e = reinterpret_cast<const SfoEntry*>(b.data() + table + i * sizeof(SfoEntry));
        size_t kp = static_cast<size_t>(h->key_table_offset) + e->key_offset;
        size_t dp = static_cast<size_t>(h->data_table_offset) + e->data_offset;
        if (kp >= b.size() || dp >= b.size()) continue;
        const char* key = reinterpret_cast<const char*>(b.data() + kp);
        size_t keymax = b.size() - kp;
        size_t keylen = strnlen(key, keymax);
        if (keylen == keymax || strcmp(key, wanted) != 0) continue;
        size_t remain = b.size() - dp;
        size_t len = static_cast<size_t>(e->length) < remain ? static_cast<size_t>(e->length) : remain;
        if (len == 0) return {};
        const char* v = reinterpret_cast<const char*>(b.data() + dp);
        size_t n = strnlen(v, len);
        return std::string(v, n);
    }
    return {};
}

static void refresh_games() {
    std::vector<GameInfo> fresh;
    DIR* d = opendir("/user/app");
    if (!d) {
        pthread_mutex_lock(&g_lock);
        g_games.clear();
        pthread_mutex_unlock(&g_lock);
        return;
    }
    while (dirent* ent = readdir(d)) {
        std::string tid = ent->d_name;
        if (tid == "." || tid == ".." || tid.size() < 8 || tid.size() > 16) continue;
        bool ok = true;
        for (size_t ci = 0; ci < tid.size(); ++ci) {
            unsigned char c = static_cast<unsigned char>(tid[ci]);
            if (!(std::isalnum(c) || c == '_' || c == '-')) { ok = false; break; }
        }
        if (!ok) continue;
        std::vector<uint8_t> sfo;
        std::string path = "/user/appmeta/" + tid + "/param.sfo";
        if (!read_file(path, sfo)) continue;
        GameInfo g;
        g.title_id = sfo_get(sfo, "TITLE_ID");
        if (g.title_id.empty()) g.title_id = tid;
        g.title = sfo_get(sfo, "TITLE");
        if (g.title.empty()) g.title = g.title_id;
        g.version = sfo_get(sfo, "APP_VER");
        g.category = sfo_get(sfo, "CATEGORY");
        fresh.push_back(std::move(g));
    }
    closedir(d);
    pthread_mutex_lock(&g_lock);
    g_games.swap(fresh);
    pthread_mutex_unlock(&g_lock);
}

static bool bgft_init_once() {
    if (g_bgft_ready) return true;
    g_bgft_heap = malloc(kBgftHeapSize);
    if (!g_bgft_heap) return false;
    memset(g_bgft_heap, 0, kBgftHeapSize);
    TtgBgftInitParams p{};
    p.heap = g_bgft_heap;
    p.heapSize = kBgftHeapSize;
    int ret = sceBgftServiceIntInit(&p);
    if (ret != 0) {
        free(g_bgft_heap);
        g_bgft_heap = nullptr;
        return false;
    }
    g_bgft_ready = true;
    return true;
}

static QueueItem* find_item_locked(uint64_t id) {
    for (auto& q : g_queue) if (q.id == id) return &q;
    return nullptr;
}

static bool start_head_locked() {
    if (!bgft_init_once()) return false;
    for (auto& q : g_queue) {
        if (q.state == QueueState::Active || q.state == QueueState::Starting || q.state == QueueState::Paused) {
            return true;
        }
    }
    for (auto& q : g_queue) {
        if (q.state != QueueState::Queued) continue;
        q.state = QueueState::Starting;
        TtgBgftDownloadParam p{};
        p.userId = g_user_id;
        p.entitlementType = 5;
        p.id = "";
        p.contentUrl = q.url.c_str();
        p.contentExUrl = "";
        p.contentName = q.name.c_str();
        p.iconPath = "/app0/sce_sys/icon0.png";
        p.skuId = "";
        p.option = kBgftDisableCdnQueryParam;
        p.playgoScenarioId = "0";
        p.releaseDate = "";
        p.packageType = "";
        p.packageSubType = "";
        p.packageSize = static_cast<unsigned long>(q.size);
        int task = -1;
        int ret = sceBgftServiceIntDebugDownloadRegisterPkg(&p, &task);
        if (ret != 0) {
            q.state = QueueState::Error;
            q.error = ret;
            return false;
        }
        ret = sceBgftServiceDownloadStartTask(task);
        if (ret != 0) {
            q.state = QueueState::Error;
            q.error = ret;
            q.task_id = task;
            return false;
        }
        q.task_id = task;
        q.state = QueueState::Active;
        return true;
    }
    return true;
}

static void poll_bgft_locked() {
    for (auto& q : g_queue) {
        if (q.task_id < 0) continue;
        if (q.state != QueueState::Active && q.state != QueueState::Paused && q.state != QueueState::Starting) continue;
        TtgBgftTaskProgress p{};
        int ret = sceBgftServiceDownloadGetProgress(q.task_id, &p);
        if (ret != 0) {
            q.error = ret;
            continue;
        }
        q.error = p.errorResult;
        q.transferred = p.transferredTotal ? p.transferredTotal : p.transferred;
        q.total = p.lengthTotal ? p.lengthTotal : p.length;
        if (q.total) q.percent = static_cast<int>((q.transferred * 100ULL) / q.total);
        if (p.errorResult != 0) {
            q.state = QueueState::Error;
        } else if (q.total && q.transferred >= q.total) {
            q.percent = 100;
            q.state = QueueState::Completed;
        }
    }
    start_head_locked();
}

static void* queue_worker(void*) {
    while (g_running) {
        pthread_mutex_lock(&g_lock);
        poll_bgft_locked();
        pthread_mutex_unlock(&g_lock);
        usleep(350000);
    }
    return nullptr;
}

static std::string query_param(const std::string& path, const std::string& key) {
    auto q = path.find('?');
    if (q == std::string::npos) return {};
    std::string k = key + "=";
    size_t p = path.find(k, q + 1);
    if (p == std::string::npos) return {};
    p += k.size();
    size_t e = path.find('&', p);
    return path.substr(p, e == std::string::npos ? std::string::npos : e - p);
}

static std::string json_string(const std::string& body, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    size_t p = body.find(needle);
    if (p == std::string::npos) return {};
    p = body.find(':', p + needle.size());
    if (p == std::string::npos) return {};
    p = body.find('"', p + 1);
    if (p == std::string::npos) return {};
    ++p;
    std::string out;
    bool esc = false;
    for (; p < body.size(); ++p) {
        char c = body[p];
        if (esc) {
            switch (c) {
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                default: out.push_back(c); break;
            }
            esc = false;
        } else if (c == '\\') {
            esc = true;
        } else if (c == '"') {
            break;
        } else {
            out.push_back(c);
        }
    }
    return out;
}

static uint64_t json_u64(const std::string& body, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    size_t p = body.find(needle);
    if (p == std::string::npos) return 0;
    p = body.find(':', p + needle.size());
    if (p == std::string::npos) return 0;
    ++p;
    while (p < body.size() && std::isspace(static_cast<unsigned char>(body[p]))) ++p;
    uint64_t v = 0;
    while (p < body.size() && std::isdigit(static_cast<unsigned char>(body[p]))) {
        v = v * 10 + static_cast<unsigned>(body[p] - '0');
        ++p;
    }
    return v;
}

static std::string make_token(const std::string& nonce) {
    uint64_t t = static_cast<uint64_t>(SDL_GetTicks());
    uint64_t h = 1469598103934665603ULL;
    for (char c : nonce) {
        h ^= static_cast<unsigned char>(c);
        h *= 1099511628211ULL;
    }
    h ^= t;
    h *= 1099511628211ULL;
    char b[80];
    snprintf(b, sizeof(b), "insync-%016llx-%016llx",
             static_cast<unsigned long long>(h),
             static_cast<unsigned long long>(t));
    return b;
}

static bool authorized(const std::string& req) {
    size_t p = req.find("\r\nAuthorization:");
    if (p == std::string::npos) p = req.find("\nauthorization:");
    if (p == std::string::npos) return false;
    size_t b = req.find("Bearer ", p);
    if (b == std::string::npos) return false;
    b += 7;
    size_t e = req.find_first_of("\r\n", b);
    std::string token = req.substr(b, e == std::string::npos ? std::string::npos : e - b);
    pthread_mutex_lock(&g_lock);
    bool ok = !g_pair.token.empty() && token == g_pair.token;
    pthread_mutex_unlock(&g_lock);
    return ok;
}

static std::string num_u64(uint64_t v) {
    char b[32];
    snprintf(b, sizeof(b), "%llu", static_cast<unsigned long long>(v));
    return b;
}
static std::string num_i64(long long v) {
    char b[32];
    snprintf(b, sizeof(b), "%lld", v);
    return b;
}
static std::string queue_json_locked() {
    std::string o = "{\"items\":[";
    for (size_t i = 0; i < g_queue.size(); ++i) {
        const auto& q = g_queue[i];
        if (i) o += ",";
        o += "{\"id\":" + num_u64(q.id)
          + ",\"name\":\"" + json_escape(q.name)
          + "\",\"url\":\"" + json_escape(q.url)
          + "\",\"size\":" + num_u64(q.size)
          + ",\"state\":\"" + state_name(q.state)
          + "\",\"task_id\":" + num_i64(q.task_id)
          + ",\"percent\":" + num_i64(q.percent)
          + ",\"transferred\":" + num_u64(q.transferred)
          + ",\"total\":" + num_u64(q.total)
          + ",\"error\":" + num_i64(q.error)
          + "}";
    }
    o += "]}";
    return o;
}

static std::string games_json_locked() {
    std::string o = "{\"games\":[";
    for (size_t i = 0; i < g_games.size(); ++i) {
        const auto& g = g_games[i];
        if (i) o += ",";
        o += "{\"title_id\":\"" + json_escape(g.title_id)
          + "\",\"title\":\"" + json_escape(g.title)
          + "\",\"version\":\"" + json_escape(g.version)
          + "\",\"category\":\"" + json_escape(g.category)
          + "\"}";
    }
    o += "]}";
    return o;
}

static void send_http(int fd, int status, const std::string& body) {
    const char* msg = status == 200 ? "OK" : status == 202 ? "Accepted" :
                      status == 401 ? "Unauthorized" : status == 404 ? "Not Found" : "Bad Request";
    char head[1024];
    int hn = snprintf(head, sizeof(head),
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %llu\r\n"
        "Connection: close\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Headers: Authorization, Content-Type\r\n"
        "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n\r\n",
        status, msg, static_cast<unsigned long long>(body.size()));
    std::string out(head, hn > 0 ? static_cast<size_t>(hn) : 0);
    out += body;
    const char* p = out.data();
    size_t left = out.size();
    while (left) {
        ssize_t n = write(fd, p, left);
        if (n <= 0) break;
        p += n;
        left -= static_cast<size_t>(n);
    }
}

static bool parse_request(int fd, std::string& req) {
    char buf[8192];
    req.clear();
    size_t need = 0;
    while (req.size() < 1024 * 1024) {
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n <= 0) break;
        req.append(buf, static_cast<size_t>(n));
        size_t header_end = req.find("\r\n\r\n");
        if (header_end != std::string::npos) {
            if (!need) {
                size_t cl = req.find("Content-Length:");
                if (cl != std::string::npos && cl < header_end) {
                    cl += strlen("Content-Length:");
                    while (cl < req.size() && std::isspace(static_cast<unsigned char>(req[cl]))) ++cl;
                    need = static_cast<size_t>(strtoull(req.c_str() + cl, nullptr, 10));
                }
            }
            size_t have = req.size() - (header_end + 4);
            if (have >= need) return true;
        }
    }
    return !req.empty();
}

static bool parse_action_path(const std::string& path, uint64_t& id, std::string& action) {
    const std::string prefix = "/v1/queue/";
    if (path.compare(0, prefix.size(), prefix) != 0) return false;
    size_t p = prefix.size();
    size_t slash = path.find('/', p);
    if (slash == std::string::npos) return false;
    id = strtoull(path.substr(p, slash - p).c_str(), nullptr, 10);
    action = path.substr(slash + 1);
    size_t q = action.find('?');
    if (q != std::string::npos) action.resize(q);
    return id != 0 && !action.empty();
}

static std::string request_path(const std::string& req, std::string& method) {
    size_t e = req.find("\r\n");
    std::string line = req.substr(0, e);
    size_t a = line.find(' ');
    size_t b = line.find(' ', a == std::string::npos ? 0 : a + 1);
    if (a == std::string::npos || b == std::string::npos) return {};
    method = line.substr(0, a);
    return line.substr(a + 1, b - a - 1);
}

static std::string request_body(const std::string& req) {
    size_t p = req.find("\r\n\r\n");
    return p == std::string::npos ? std::string() : req.substr(p + 4);
}

static void handle_client(int fd) {
    std::string req;
    if (!parse_request(fd, req)) {
        send_http(fd, 400, "{\"error\":\"bad_request\"}");
        return;
    }
    std::string method;
    std::string path = request_path(req, method);
    std::string body = request_body(req);

    if (method == "OPTIONS") {
        send_http(fd, 200, "{}");
        return;
    }

    if (method == "POST" && path == "/v1/pair/request") {
        std::string rid = json_string(body, "request_id");
        std::string client = json_string(body, "client");
        if (rid.empty()) {
            send_http(fd, 400, "{\"error\":\"request_id_required\"}");
            return;
        }
        pthread_mutex_lock(&g_lock);
        g_pair.request_id = rid;
        g_pair.client = client.empty() ? "iNSync" : client;
        g_pair.waiting = true;
        g_pair.approved = false;
        g_pair.rejected = false;
        pthread_mutex_unlock(&g_lock);
        send_http(fd, 202, "{\"status\":\"waiting\",\"physical_approval\":true}");
        return;
    }

    if (method == "GET" && path.find("/v1/pair/status") == 0) {
        std::string rid = query_param(path, "request_id");
        pthread_mutex_lock(&g_lock);
        std::string out;
        if (rid.empty() || rid != g_pair.request_id) {
            out = "{\"status\":\"unknown\"}";
        } else if (g_pair.approved) {
            out = "{\"status\":\"approved\",\"token\":\"" + json_escape(g_pair.token) + "\"}";
        } else if (g_pair.rejected) {
            out = "{\"status\":\"rejected\"}";
        } else {
            out = "{\"status\":\"waiting\"}";
        }
        pthread_mutex_unlock(&g_lock);
        send_http(fd, 200, out);
        return;
    }

    if (!authorized(req)) {
        send_http(fd, 401, "{\"error\":\"unauthorized\"}");
        return;
    }

    if (method == "GET" && path == "/v1/status") {
        pthread_mutex_lock(&g_lock);
        size_t active = 0, pending = 0;
        for (const auto& q : g_queue) {
            if (q.state == QueueState::Active || q.state == QueueState::Paused || q.state == QueueState::Starting) ++active;
            if (q.state == QueueState::Queued) ++pending;
        }
        std::string out = std::string("{\"ok\":true,\"version\":\"") + kVersion
          + "\",\"title_id\":\"" + kTitleId
          + "\",\"paired\":true,\"queue_count\":" + num_u64(g_queue.size())
          + ",\"active\":" + num_u64(active)
          + ",\"pending\":" + num_u64(pending)
          + ",\"games\":" + num_u64(g_games.size())
          + "}";
        pthread_mutex_unlock(&g_lock);
        send_http(fd, 200, out);
        return;
    }

    if (method == "GET" && path == "/v1/queue") {
        pthread_mutex_lock(&g_lock);
        std::string out = queue_json_locked();
        pthread_mutex_unlock(&g_lock);
        send_http(fd, 200, out);
        return;
    }

    if (method == "GET" && path == "/v1/games") {
        refresh_games();
        pthread_mutex_lock(&g_lock);
        std::string out = games_json_locked();
        pthread_mutex_unlock(&g_lock);
        send_http(fd, 200, out);
        return;
    }

    if (method == "POST" && path == "/v1/queue/add") {
        std::string url = json_string(body, "url");
        std::string name = json_string(body, "name");
        uint64_t size = json_u64(body, "size");
        if (url.rfind("http://", 0) != 0 && url.rfind("https://", 0) != 0) {
            send_http(fd, 400, "{\"error\":\"http_url_required\"}");
            return;
        }
        if (name.empty()) name = "package.pkg";
        pthread_mutex_lock(&g_lock);
        QueueItem q;
        q.id = g_next_id++;
        q.name = name;
        q.url = url;
        q.size = size;
        g_queue.push_back(q);
        start_head_locked();
        uint64_t id = q.id;
        pthread_mutex_unlock(&g_lock);
        send_http(fd, 202, std::string("{\"queued\":true,\"id\":") + num_u64(id) + "}");
        return;
    }

    uint64_t id = 0;
    std::string action;
    if (method == "POST" && parse_action_path(path, id, action)) {
        pthread_mutex_lock(&g_lock);
        QueueItem* q = find_item_locked(id);
        if (!q) {
            pthread_mutex_unlock(&g_lock);
            send_http(fd, 404, "{\"error\":\"queue_item_not_found\"}");
            return;
        }
        int ret = 0;
        if (action == "pause" && q->task_id >= 0) {
            ret = sceBgftServiceDownloadPauseTask(q->task_id);
            if (!ret) q->state = QueueState::Paused;
        } else if (action == "resume" && q->task_id >= 0) {
            ret = sceBgftServiceDownloadResumeTask(q->task_id);
            if (!ret) q->state = QueueState::Active;
        } else if (action == "cancel") {
            if (q->task_id >= 0) ret = sceBgftServiceDownloadStopTask(q->task_id);
            if (!ret) q->state = QueueState::Cancelled;
            start_head_locked();
        } else if (action == "top") {
            size_t pos = g_queue.size();
            for (size_t i = 0; i < g_queue.size(); ++i) if (g_queue[i].id == id) { pos = i; break; }
            if (pos < g_queue.size() && g_queue[pos].state == QueueState::Queued) {
                QueueItem moved = g_queue[pos];
                g_queue.erase(g_queue.begin() + static_cast<long>(pos));
                size_t insert = 0;
                while (insert < g_queue.size() &&
                       (g_queue[insert].state == QueueState::Active ||
                        g_queue[insert].state == QueueState::Paused ||
                        g_queue[insert].state == QueueState::Starting)) {
                    ++insert;
                }
                g_queue.insert(g_queue.begin() + static_cast<long>(insert), moved);
            }
        } else {
            pthread_mutex_unlock(&g_lock);
            send_http(fd, 400, "{\"error\":\"unsupported_action\"}");
            return;
        }
        int err = ret;
        pthread_mutex_unlock(&g_lock);
        std::string out = std::string("{\"ok\":") + (err == 0 ? "true" : "false")
                        + ",\"result\":" + num_i64(err) + "}";
        send_http(fd, err == 0 ? 200 : 400, out);
        return;
    }

    send_http(fd, 404, "{\"error\":\"not_found\"}");
}

static void* http_server(void*) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return nullptr;
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(kPort);
    if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        close(s);
        return nullptr;
    }
    if (listen(s, 8) != 0) {
        close(s);
        return nullptr;
    }
    while (g_running) {
        sockaddr_in peer{};
        socklen_t n = sizeof(peer);
        int c = accept(s, reinterpret_cast<sockaddr*>(&peer), &n);
        if (c < 0) {
            if (errno == EINTR) continue;
            usleep(100000);
            continue;
        }
        handle_client(c);
        close(c);
    }
    close(s);
    return nullptr;
}

static FT_Library g_ft = nullptr;
static FT_Face g_font = nullptr;

static bool init_font() {
    int r = sceSysmoduleLoadModule(ORBIS_SYSMODULE_FREETYPE_OL);
    if (r < 0) return false;
    if (FT_Init_FreeType(&g_ft) != 0) return false;
    if (FT_New_Face(g_ft, "/app0/assets/fonts/Gontserrat-Regular.ttf", 0, &g_font) != 0) return false;
    return FT_Set_Pixel_Sizes(g_font, 0, 32) == 0;
}

static void draw_text(SDL_Renderer* renderer, int x, int y, const std::string& text, int px = 32,
                      SDL_Color color = SDL_Color{235,235,240,255}) {
    if (!g_font) return;
    FT_Set_Pixel_Sizes(g_font, 0, px);
    int pen = x;
    for (unsigned char ch : text) {
        if (ch == '\n') { y += px + 8; pen = x; continue; }
        FT_UInt idx = FT_Get_Char_Index(g_font, ch);
        if (FT_Load_Glyph(g_font, idx, FT_LOAD_DEFAULT) != 0) continue;
        if (FT_Render_Glyph(g_font->glyph, ft_render_mode_normal) != 0) continue;
        FT_GlyphSlot g = g_font->glyph;
        if (g->bitmap.width && g->bitmap.rows) {
            SDL_Surface* surf = SDL_CreateRGBSurface(0, g->bitmap.width, g->bitmap.rows, 32,
                                                     0x00FF0000,0x0000FF00,0x000000FF,0xFF000000);
            if (surf) {
                SDL_LockSurface(surf);
                auto* pix = static_cast<uint32_t*>(surf->pixels);
                for (int row = 0; row < g->bitmap.rows; ++row) {
                    for (int col = 0; col < g->bitmap.width; ++col) {
                        uint8_t a = g->bitmap.buffer[row * g->bitmap.pitch + col];
                        pix[row * (surf->pitch / 4) + col] =
                            (static_cast<uint32_t>(a) << 24) |
                            (static_cast<uint32_t>(color.r) << 16) |
                            (static_cast<uint32_t>(color.g) << 8) |
                            color.b;
                    }
                }
                SDL_UnlockSurface(surf);
                SDL_Texture* tex = SDL_CreateTextureFromSurface(renderer, surf);
                if (tex) {
                    SDL_SetTextureBlendMode(tex, SDL_BLENDMODE_BLEND);
                    SDL_Rect dst{pen + g->bitmap_left, y + px - g->bitmap_top,
                                 static_cast<int>(g->bitmap.width), static_cast<int>(g->bitmap.rows)};
                    SDL_RenderCopy(renderer, tex, nullptr, &dst);
                    SDL_DestroyTexture(tex);
                }
                SDL_FreeSurface(surf);
            }
        }
        pen += g->advance.x >> 6;
    }
}

static void fill(SDL_Renderer* r, int x, int y, int w, int h, Uint8 rr, Uint8 gg, Uint8 bb, Uint8 aa=255) {
    SDL_Rect rc{x,y,w,h};
    SDL_SetRenderDrawColor(r,rr,gg,bb,aa);
    SDL_RenderFillRect(r,&rc);
}

static int init_pad() {
    OrbisUserServiceInitializeParams p{};
    p.priority = ORBIS_KERNEL_PRIO_FIFO_LOWEST;
    sceUserServiceInitialize(&p);
    sceUserServiceGetInitialUser(&g_user_id);
    if (scePadInit() != 0) return -1;
    return scePadOpen(g_user_id, 0, 0, nullptr);
}

static void render_ui(SDL_Renderer* r, int page, int selected) {
    SDL_SetRenderDrawColor(r, 10, 12, 18, 255);
    SDL_RenderClear(r);
    fill(r,0,0,kFrameW,110,22,26,37);
    draw_text(r,70,25,"iNSync Companion",48);
    draw_text(r,70,78,"THETECHGUY | PS4 package manager",24,SDL_Color{155,165,180,255});

    pthread_mutex_lock(&g_lock);
    std::string pair_line = g_pair.waiting && !g_pair.approved
        ? ("PAIR REQUEST: " + g_pair.client + "   [X] Approve")
        : (g_pair.approved ? "Paired with iNSync" : "Waiting for iNSync pair request");
    draw_text(r,1120,40,pair_line,24,g_pair.waiting && !g_pair.approved
              ? SDL_Color{255,210,100,255}:SDL_Color{130,220,165,255});

    draw_text(r,70,145,page==0?"QUEUE":"INSTALLED GAMES",34);
    draw_text(r,70,195,page==0
              ? "[UP/DOWN] Select   [TRIANGLE] Pause/Resume   [SQUARE] Top   [CIRCLE] Cancel   [R1] Games"
              : "[UP/DOWN] Scroll   [L1] Queue",22,SDL_Color{145,155,175,255});

    if (page == 0) {
        if (g_queue.empty()) {
            draw_text(r,70,300,"No packages queued.",32,SDL_Color{150,160,175,255});
        } else {
            int start = selected - 6; if (start < 0) start = 0;
            int end = static_cast<int>(g_queue.size()); if (end > start + 12) end = start + 12;
            for (int i = start; i < end; ++i) {
                int y = 260 + (i-start)*64;
                if (i == selected) fill(r,50,y-8,1820,56,35,42,58);
                const auto& q = g_queue[i];
                std::string left = q.name + "  [" + state_name(q.state) + "]";
                draw_text(r,80,y,left,25);
                char pct[24];
                snprintf(pct, sizeof(pct), "%d%%", q.percent);
                draw_text(r,1660,y,pct,25,q.state==QueueState::Error
                          ? SDL_Color{255,110,110,255}:SDL_Color{130,220,165,255});
                fill(r,1180,y+10,420,12,45,50,65);
                int bw = q.percent*420/100; if (bw < 0) bw = 0; if (bw > 420) bw = 420;
                fill(r,1180,y+10,bw,12,95,205,150);
            }
        }
    } else {
        if (g_games.empty()) {
            draw_text(r,70,300,"No installed-game metadata found yet.",32,SDL_Color{150,160,175,255});
        } else {
            int start = selected - 6; if (start < 0) start = 0;
            int end = static_cast<int>(g_games.size()); if (end > start + 12) end = start + 12;
            for (int i = start; i < end; ++i) {
                int y = 260 + (i-start)*64;
                if (i == selected) fill(r,50,y-8,1820,56,35,42,58);
                const auto& g = g_games[i];
                draw_text(r,80,y,g.title,25);
                draw_text(r,1290,y,g.title_id + "  v" + g.version,23,SDL_Color{155,165,180,255});
            }
        }
    }
    pthread_mutex_unlock(&g_lock);

    draw_text(r,70,1010,"API 49560 | Shared queue: PC and console stay synchronized",22,SDL_Color{120,130,150,255});
    SDL_RenderPresent(r);
}

int main(int, char**) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    if (static_cast<int32_t>(sceSysmoduleLoadModuleInternal(ORBIS_SYSMODULE_INTERNAL_APP_INST_UTIL)) < 0) return 1;
    if (static_cast<int32_t>(sceSysmoduleLoadModuleInternal(ORBIS_SYSMODULE_INTERNAL_BGFT)) < 0) return 2;
    if (sceAppInstUtilInitialize() != 0) return 3;
    if (!bgft_init_once()) return 4;

    if (SDL_Init(SDL_INIT_VIDEO) != 0) return 5;
    if (!init_font()) return 6;

    SDL_Window* w = SDL_CreateWindow("iNSync Companion", SDL_WINDOWPOS_UNDEFINED,
                                     SDL_WINDOWPOS_UNDEFINED, kFrameW, kFrameH, 0);
    if (!w) return 7;
    SDL_Renderer* renderer = SDL_CreateRenderer(w, -1, SDL_RENDERER_SOFTWARE);
    if (!renderer) {
        SDL_Surface* s = SDL_GetWindowSurface(w);
        renderer = SDL_CreateSoftwareRenderer(s);
    }
    if (!renderer) return 8;
    SDL_SetRenderDrawBlendMode(renderer, SDL_BLENDMODE_BLEND);

    refresh_games();

    int pad = init_pad();
    pthread_t http_thread{}, worker_thread{};
    pthread_create(&http_thread, nullptr, http_server, nullptr);
    pthread_create(&worker_thread, nullptr, queue_worker, nullptr);

    int page = 0;
    int selected = 0;
    uint32_t prev = 0;
    uint64_t last_games = 0;

    while (g_running) {
        OrbisPadData pd{};
        uint32_t buttons = 0;
        if (pad >= 0 && scePadReadState(pad, &pd) == 0) buttons = pd.buttons;
        uint32_t pressed = buttons & ~prev;
        prev = buttons;

        if (pressed & ORBIS_PAD_BUTTON_R1) {
            page = 1;
            selected = 0;
            refresh_games();
        }
        if (pressed & ORBIS_PAD_BUTTON_L1) {
            page = 0;
            selected = 0;
        }
        pthread_mutex_lock(&g_lock);
        int count = page == 0 ? static_cast<int>(g_queue.size()) : static_cast<int>(g_games.size());
        pthread_mutex_unlock(&g_lock);

        if ((pressed & ORBIS_PAD_BUTTON_UP) && selected > 0) --selected;
        if ((pressed & ORBIS_PAD_BUTTON_DOWN) && selected + 1 < count) ++selected;

        if (pressed & ORBIS_PAD_BUTTON_CROSS) {
            pthread_mutex_lock(&g_lock);
            if (g_pair.waiting && !g_pair.approved) {
                g_pair.approved = true;
                g_pair.rejected = false;
                g_pair.waiting = false;
                g_pair.token = make_token(g_pair.request_id);
            }
            pthread_mutex_unlock(&g_lock);
        }

        if (page == 0 && count > 0) {
            pthread_mutex_lock(&g_lock);
            if (selected >= 0 && selected < static_cast<int>(g_queue.size())) {
                QueueItem& q = g_queue[selected];
                if (pressed & ORBIS_PAD_BUTTON_TRIANGLE) {
                    if (q.state == QueueState::Paused && q.task_id >= 0) {
                        if (sceBgftServiceDownloadResumeTask(q.task_id) == 0) q.state = QueueState::Active;
                    } else if (q.state == QueueState::Active && q.task_id >= 0) {
                        if (sceBgftServiceDownloadPauseTask(q.task_id) == 0) q.state = QueueState::Paused;
                    }
                }
                if (pressed & ORBIS_PAD_BUTTON_CIRCLE) {
                    if (q.task_id >= 0) sceBgftServiceDownloadStopTask(q.task_id);
                    q.state = QueueState::Cancelled;
                    start_head_locked();
                }
                if ((pressed & ORBIS_PAD_BUTTON_SQUARE) && q.state == QueueState::Queued) {
                    QueueItem moved = q;
                    g_queue.erase(g_queue.begin() + selected);
                    size_t insert = 0;
                    while (insert < g_queue.size() &&
                           (g_queue[insert].state == QueueState::Active ||
                            g_queue[insert].state == QueueState::Paused ||
                            g_queue[insert].state == QueueState::Starting)) ++insert;
                    g_queue.insert(g_queue.begin() + static_cast<long>(insert), moved);
                    selected = static_cast<int>(insert);
                }
            }
            pthread_mutex_unlock(&g_lock);
        }

        uint64_t now = SDL_GetTicks();
        if (page == 1 && now - last_games > 15000) {
            refresh_games();
            last_games = now;
        }

        render_ui(renderer, page, selected);
        SDL_Delay(33);
    }

    g_running = false;
    pthread_cancel(http_thread);
    pthread_join(http_thread, nullptr);
    pthread_join(worker_thread, nullptr);

    if (pad >= 0) scePadClose(pad);
    if (g_font) FT_Done_Face(g_font);
    if (g_ft) FT_Done_FreeType(g_ft);
    SDL_DestroyRenderer(renderer);
    SDL_DestroyWindow(w);
    SDL_Quit();

    if (g_bgft_ready) sceBgftServiceIntTerm();
    if (g_bgft_heap) free(g_bgft_heap);
    sceAppInstUtilTerminate();
    sceSysmoduleUnloadModuleInternal(ORBIS_SYSMODULE_INTERNAL_BGFT);
    sceSysmoduleUnloadModuleInternal(ORBIS_SYSMODULE_INTERNAL_APP_INST_UTIL);
    return 0;
}
