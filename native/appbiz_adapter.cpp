// A normal build pulls in the Windows SDK for the SEH macros, the Win32 scalar
// types and the DllMain signature. When only the cached MSVC compiler bits are
// available (no Windows SDK installed), APPBIZ_ADAPTER_NO_SDK_HEADERS lets this
// file build from a minimal declaration set instead: __try/__except and
// GetExceptionCode are compiler intrinsics, and nothing else from the SDK is
// used here.
#if defined(APPBIZ_ADAPTER_NO_SDK_HEADERS)
using BOOL = int;
using DWORD = unsigned long;
using LPVOID = void*;
using HINSTANCE = void*;
#define WINAPI __stdcall
#define EXCEPTION_EXECUTE_HANDLER 1
#define TRUE 1
#define FALSE 0
#else
#include <Windows.h>
#endif

#include <array>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <unordered_map>


namespace {

constexpr std::uintptr_t kMessageBizOffset = 0x578;

struct AppBizProfile {
    std::uintptr_t app_message_service_vtable_rva;
    std::uintptr_t message_biz_vtable_rva;
    std::uintptr_t message_biz_send_text_rva;
};

constexpr std::array<AppBizProfile, 3> kAppBizProfiles{{
    {0x18AF478, 0x18AD4F8, 0xA59120},  // 9.97.59N
    {0x18BDEE8, 0x18BBF68, 0xA64BF0},  // 9.97.74N
    {0x18C4C78, 0x18C2CF8, 0xA66940},  // 9.97.81N
}};

struct DummyJsonValue {
    alignas(void*) unsigned char storage[32]{};
};

using EmptyMetadata = std::unordered_map<std::string, DummyJsonValue>;
struct ResultCode {};
struct Message {};
using ResultCallback = std::function<void(const ResultCode&, const Message&)>;
constexpr std::int32_t kUnreadableResultCode = (-2147483647 - 1);
using MessageBizSendTextFunction = void(__fastcall *)(
    void*,
    const std::string&,
    const std::string&,
    const std::string&,
    const EmptyMetadata&,
    const ResultCallback&);

struct SendReceipt {
    bool callback_invoked = false;
    std::int32_t result_code = kUnreadableResultCode;
};

std::mutex g_receipts_mutex;
std::unordered_map<std::uint64_t, SendReceipt> g_receipts;

std::int32_t read_result_code(const ResultCode& result) noexcept {
    __try {
        const auto* bytes = reinterpret_cast<const unsigned char*>(&result);
        return *reinterpret_cast<const std::int32_t*>(bytes + 8);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return kUnreadableResultCode;
    }
}

int call_send_raw(
    MessageBizSendTextFunction function,
    void* message_biz,
    const std::string* arg1,
    const std::string* arg2,
    const std::string* arg3,
    const EmptyMetadata* metadata,
    const ResultCallback* callback) {
    __try {
        function(message_biz, *arg1, *arg2, *arg3, *metadata, *callback);
        return 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -3;
    }
}

int send_text_impl(
    void* service,
    void* appbiz_module_base,
    const char* arg1_utf8,
    const char* arg2_utf8,
    const char* arg3_utf8,
    std::uint64_t receipt_token) {
    if (!service || !appbiz_module_base || !arg1_utf8 || !arg2_utf8 || !arg3_utf8) {
        return -1;
    }
    const auto base = reinterpret_cast<std::uintptr_t>(appbiz_module_base);
    const auto service_vtable = *reinterpret_cast<const std::uintptr_t*>(service);
    const AppBizProfile* profile = nullptr;
    for (const auto& candidate : kAppBizProfiles) {
        if (service_vtable == base + candidate.app_message_service_vtable_rva) {
            profile = &candidate;
            break;
        }
    }
    if (!profile) {
        return -2;
    }
    auto* const message_biz = *reinterpret_cast<void**>(
        reinterpret_cast<std::uintptr_t>(service) + kMessageBizOffset);
    if (!message_biz ||
        *reinterpret_cast<const std::uintptr_t*>(message_biz) !=
            base + profile->message_biz_vtable_rva) {
        return -5;
    }

    try {
        const std::string arg1(arg1_utf8);
        const std::string arg2(arg2_utf8);
        const std::string arg3(arg3_utf8);
        const EmptyMetadata metadata;
        ResultCallback callback;
        if (receipt_token) {
            {
                std::lock_guard<std::mutex> lock(g_receipts_mutex);
                g_receipts[receipt_token] = SendReceipt{};
            }
            callback = [receipt_token](const ResultCode& result, const Message&) noexcept {
                const auto code = read_result_code(result);
                std::lock_guard<std::mutex> lock(g_receipts_mutex);
                const auto found = g_receipts.find(receipt_token);
                if (found != g_receipts.end() && !found->second.callback_invoked) {
                    found->second.callback_invoked = true;
                    found->second.result_code = code;
                }
            };
        } else {
            callback = [](const ResultCode&, const Message&) noexcept {};
        }
        const auto function = reinterpret_cast<MessageBizSendTextFunction>(
            base + profile->message_biz_send_text_rva);
        const auto result = call_send_raw(
            function, message_biz, &arg1, &arg2, &arg3, &metadata, &callback);
        if (result != 0 && receipt_token) {
            std::lock_guard<std::mutex> lock(g_receipts_mutex);
            g_receipts.erase(receipt_token);
        }
        return result;
    } catch (...) {
        if (receipt_token) {
            std::lock_guard<std::mutex> lock(g_receipts_mutex);
            g_receipts.erase(receipt_token);
        }
        return -4;
    }
}

}  // namespace


extern "C" __declspec(dllexport) int appbiz_adapter_layout(
    std::uint32_t* string_size,
    std::uint32_t* metadata_size,
    std::uint32_t* callback_size) {
    if (!string_size || !metadata_size || !callback_size) {
        return -1;
    }
    *string_size = static_cast<std::uint32_t>(sizeof(std::string));
    *metadata_size = static_cast<std::uint32_t>(sizeof(EmptyMetadata));
    *callback_size = static_cast<std::uint32_t>(sizeof(ResultCallback));
    return 0;
}


extern "C" __declspec(dllexport) int appbiz_send_text_v1(
    void* service,
    void* appbiz_module_base,
    const char* arg1_utf8,
    const char* arg2_utf8,
    const char* arg3_utf8) {
    return send_text_impl(
        service, appbiz_module_base, arg1_utf8, arg2_utf8, arg3_utf8, 0);
}


extern "C" __declspec(dllexport) int appbiz_send_text_v2(
    void* service,
    void* appbiz_module_base,
    const char* arg1_utf8,
    const char* arg2_utf8,
    const char* arg3_utf8,
    std::uint64_t receipt_token) {
    if (!receipt_token) {
        return -6;
    }
    return send_text_impl(
        service, appbiz_module_base, arg1_utf8, arg2_utf8, arg3_utf8, receipt_token);
}


extern "C" __declspec(dllexport) int appbiz_poll_send_result_v1(
    std::uint64_t receipt_token,
    std::int32_t* result_code) {
    if (!receipt_token || !result_code) {
        return -1;
    }
    std::lock_guard<std::mutex> lock(g_receipts_mutex);
    const auto found = g_receipts.find(receipt_token);
    if (found == g_receipts.end()) {
        return -2;
    }
    if (!found->second.callback_invoked) {
        return 0;
    }
    *result_code = found->second.result_code;
    g_receipts.erase(found);
    return 1;
}


extern "C" __declspec(dllexport) int appbiz_cancel_send_result_v1(
    std::uint64_t receipt_token) {
    if (!receipt_token) {
        return -1;
    }
    std::lock_guard<std::mutex> lock(g_receipts_mutex);
    return g_receipts.erase(receipt_token) ? 0 : -2;
}


BOOL WINAPI DllMain(HINSTANCE, DWORD, LPVOID) {
    return TRUE;
}
