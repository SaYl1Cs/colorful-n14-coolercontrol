-- Only mocks: generated handlers never see the host filesystem or ACPI.
local tests = {}
local digest = "4aab3c89fdaac8ab9e20d31cff3fe0a18b83877442520eb294429ff9237bef06"
local permission = "/run/colorful-n14-control/permission"
local smbios_path = "/sys/firmware/dmi/entries/0-0/raw"
local function smbios(major, minor)
    return string.char(0, 24) .. string.rep("\0", 20) .. string.char(major, minor)
end
local function firmware(access)
    N14 = nil
    local f = {
        files = {
            ["/sys/class/dmi/id/sys_vendor"] = "COLORFUL\n",
            ["/sys/class/dmi/id/product_name"] = "N14 24\n",
            ["/sys/class/dmi/id/bios_version"] = "2.07\n",
            [smbios_path] = smbios(2, 0),
            [permission] = digest .. " 55 71 " .. (access or "control") .. "\n",
        },
        brand = {0x55, 0x71}, targets = {[0x54] = 70, [0x55] = 70},
        cpu_tach = 68, value = 35, calls = {}, responses = {}, failures = {},
        write_response = 0,
    }
    io = {open = function(path, mode)
        assert(mode == "rb")
        local text = f.files[path]
        if not text then return nil end
        return {read = function(_, pattern) assert(pattern == "*a"); return text end,
                close = function() end}
    end}
    get_value = function() return f.value end
    acpi_call = function(command)
        local prefix = "\\_SB.AMW0.WMBA 0 2 b"
        assert(command:sub(1, #prefix) == prefix, "wrong ACPI method")
        local encoded = command:sub(#prefix + 1)
        assert(#encoded == 48 and encoded:match("^%x+$"), "bad packet")
        local bytes = {}
        for offset = 1, #encoded, 2 do
            bytes[#bytes + 1] = tonumber(encoded:sub(offset, offset + 1), 16)
        end
        assert(string.char(table.unpack(bytes, 1, 4)) == "BYDL")
        local port = bytes[5] + bytes[6] * 256
        assert(port == 0x2e or port == 0x302, "only pinned indexed I/O")
        for index = 7, 12 do assert(bytes[index] == 0) end
        assert(bytes[13] == 1 and bytes[15] == 8)
        for index = 18, 24 do assert(bytes[index] == 0) end
        local write, index, value = bytes[14], bytes[16], bytes[17]
        assert(write == 0 or write == 1)
        assert(index ~= 0x5d, "GPU TACH MUST NEVER BE ACCESSED")
        f.calls[#f.calls + 1] = {port = port, write = write, index = index, value = value}
        if write == 0 then assert(value == 0) end
        if f.failures[index] then return f.failures[index], 0 end
        if f.responses[index] ~= nil then return nil, f.responses[index] end
        if port == 0x2e then
            assert(write == 0 and (index == 0x20 or index == 0x21))
            return nil, f.brand[index - 0x1f]
        end
        if write == 1 then
            assert(index == 0x50 or index == 0x54 or index == 0x55)
            if index == 0x50 then
                assert(value == 2, "must never select Normal")
            else
                assert(value >= 28 and value <= 70, "40% floor / maximum")
                f.targets[index] = value
            end
            return nil, f.write_response
        end
        assert(index == 0x5c or index == 0x55, "only CPU tach / GPU command reads")
        return nil, index == 0x5c and f.cpu_tach or f.targets[0x55]
    end
    function f:run(fan, operation) return handlers[fan][operation]() end
    function f:writes()
        local result = {}
        for _, call in ipairs(self.calls) do
            if call.write == 1 then result[#result + 1] = call end
        end
        return result
    end
    return f
end
local function ok(error, value, expected)
    assert(error == nil, tostring(error))
    assert(value == (expected or 0), tostring(value))
end
local function fails(error, fragment)
    assert(type(error) == "string", "expected error")
    if type(fragment) == "string" then assert(error:find(fragment, 1, true), error) end
end
-- Explicit expected-value helpers avoid Lua multi-return argument ambiguity.
local function read(f, fan, expected)
    local e, v = f:run(fan, "Read")
    ok(e, v, expected)
end

function tests.real_service_order()
    local f = firmware()
    read(f, 1, 68); read(f, 2, 70)
    ok(f:run(1, "Write")); ok(f:run(2, "Write"))
    ok(f:run(1, "Reset")); ok(f:run(2, "Reset"))
    assert(f.targets[0x54] == 70 and f.targets[0x55] == 70)
end
function tests.gpu_first_bootstraps_shared_state()
    local f = firmware()
    read(f, 2, 70); read(f, 1, 68)
    assert(#f.calls == 4)
end
function tests.write_without_bootstrap_fails_closed()
    local f = firmware()
    fails(f:run(1, "Write")); fails(f:run(2, "Write"))
    ok(f:run(1, "Reset")); assert(#f.calls == 0)
end
function tests.read_only_never_writes_even_cleanup()
    local f = firmware("read")
    read(f, 1, 68); read(f, 2, 70)
    fails(f:run(1, "Write")); fails(f:run(2, "Write"))
    ok(f:run(1, "Reset")); ok(f:run(2, "Reset"))
    assert(#f:writes() == 0)
end
function tests.cpu_tach_not_target()
    local f = firmware()
    for value = 0, 70 do f.cpu_tach = value; read(f, 1, value) end
    assert(f.targets[0x54] == 70 and #f:writes() == 0)
end
function tests.gpu_reads_actual_command_not_cached_request()
    local f = firmware()
    read(f, 2, 70)
    ok(f:run(2, "Write")); assert(f.targets[0x55] == 35)
    for value = 0, 70 do f.targets[0x55] = value; read(f, 2, value) end
end
function tests.gpu_out_of_range_is_error()
    for _, value in ipairs({71, 141, 192, 255}) do
        local f = firmware(); f.targets[0x55] = value
        fails(f:run(2, "Read")); assert(#f:writes() == 0)
    end
end
function tests.cpu_tach_checks_retained()
    for _, value in ipairs({71, 141, 255}) do
        local f = firmware(); f.cpu_tach = value
        fails(f:run(1, "Read")); assert(#f:writes() == 0)
    end
end
function tests.invalid_firmware_response()
    for _, index in ipairs({0x20, 0x21, 0x5c, 0x55}) do
        for _, value in ipairs({-1, 1.5, 2147483648, math.huge, 0/0, "70", true, false}) do
            local f = firmware(); f.responses[index] = value
            fails(f:run(index == 0x55 and 2 or 1, "Read"))
            assert(#f:writes() == 0)
        end
    end
end
function tests.acpi_read_errors_propagate()
    for _, index in ipairs({0x20, 0x21, 0x5c, 0x55}) do
        local f = firmware(); f.failures[index] = "original ACPI failure"
        local e = f:run(index == 0x55 and 2 or 1, "Read")
        assert(e == "original ACPI failure" and #f:writes() == 0)
    end
end
function tests.dmi_guard_before_calls()
    for _, key in ipairs({"sys_vendor", "product_name", "bios_version"}) do
        local f = firmware(); f.files["/sys/class/dmi/id/" .. key] = "wrong"
        fails(f:run(1, "Read")); assert(#f.calls == 0)
    end
end
function tests.smbios_guard_before_calls()
    for _, raw in ipairs({false, "", smbios(1, 0), smbios(2, 1), smbios(255, 255),
                          smbios(2, 0):sub(1, 23), string.char(1) .. smbios(2, 0):sub(2),
                          string.char(0, 25) .. smbios(2, 0):sub(3)}) do
        local f = firmware(); f.files[smbios_path] = raw
        fails(f:run(1, "Read")); assert(#f.calls == 0)
    end
end
function tests.ticket_missing_invalid_or_old_rejected()
    for _, ticket in ipairs({false, "bad", digest .. " 55 71 root\n", digest .. " 55 71 control",
                             string.rep("0", 64) .. " 55 71 control\n", digest .. " 20 10 control\n"}) do
        local f = firmware(); f.files[permission] = ticket
        f.files["/run/colorful-n14-nbfc/permission"] = digest .. " 55 71 control\n"
        fails(f:run(1, "Read")); assert(#f.calls == 0)
    end
end
function tests.observed_identity_pinned()
    for _, brand in ipairs({{0, 0}, {255, 255}, {0x20, 0x10}, {0x55, 0x70}}) do
        local f = firmware(); f.brand = brand
        fails(f:run(1, "Read")); assert(#f:writes() == 0)
        ok(f:run(1, "Reset"))
    end
end
function tests.failed_guard_is_not_cached()
    local f = firmware(); f.brand = {0, 0}
    fails(f:run(1, "Read")); f.brand = {0x55, 0x71}; read(f, 1, 68)
end
function tests.independent_targets_initial_full()
    local f = firmware(); read(f, 1, 68)
    ok(f:run(1, "Write")); local writes = f:writes()
    assert(#writes == 4)
    assert(writes[1].index == 0x50 and writes[1].value == 2)
    assert(writes[2].index == 0x54 and writes[2].value == 70)
    assert(writes[3].index == 0x55 and writes[3].value == 70)
    assert(f.targets[0x54] == 35 and f.targets[0x55] == 70)
    f.value = 56; ok(f:run(2, "Write"))
    assert(f.targets[0x54] == 35 and f.targets[0x55] == 56)
end
function tests.all_targets_both_channels_clamped()
    local f = firmware(); read(f, 1, 68)
    for _, fan in ipairs({1, 2}) do
        for value = 0, 70 do
            f.value = value; ok(f:run(fan, "Write"))
            assert(f.targets[fan == 1 and 0x54 or 0x55] == math.max(28, value))
        end
    end
end
function tests.invalid_targets_no_additional_calls()
    for _, value in ipairs({-1, 71, 2.5, 0/0, math.huge, "35", true, false}) do
        local f = firmware(); read(f, 1, 68); local count = #f.calls
        f.value = value; fails(f:run(1, "Write")); assert(#f.calls == count)
    end
end
function tests.invalid_target_index_no_calls()
    local f = firmware(); read(f, 1, 68); local count = #f.calls
    fails(N14.write(0x5d, 35)); assert(#f.calls == count)
end
function tests.cleanup_without_control_is_noop()
    local f = firmware(); read(f, 1, 68); local count = #f.calls
    ok(f:run(1, "Reset")); ok(f:run(2, "Reset")); assert(#f.calls == count)
end
function tests.cleanup_full_never_normal()
    local f = firmware(); read(f, 1, 68); ok(f:run(1, "Write"))
    for _ = 1, 2 do
        ok(f:run(2, "Reset"))
        assert(f.targets[0x54] == 70 and f.targets[0x55] == 70)
    end
end
function tests.initial_full_failure_attempts_all_then_cleanup()
    for _, index in ipairs({0x50, 0x54, 0x55}) do
        local f = firmware(); read(f, 1, 68); f.failures[index] = "initial fail"
        local e = f:run(1, "Write"); assert(e == "initial fail" and #f:writes() == 3)
        f.failures[index] = nil; ok(f:run(2, "Reset"))
        assert(f.targets[0x54] == 70 and f.targets[0x55] == 70)
    end
end
function tests.later_write_failure_requests_both_full()
    for _, index in ipairs({0x50, 0x54, 0x55}) do
        local f = firmware(); read(f, 1, 68); ok(f:run(1, "Write"))
        local before = #f:writes(); f.failures[index] = "later fail"
        local e = f:run(index == 0x55 and 2 or 1, "Write")
        assert(e == "later fail")
        local writes = f:writes()
        assert(#writes >= before + 4)
        assert(writes[#writes - 2].index == 0x50 and writes[#writes - 2].value == 2)
        assert(writes[#writes - 1].index == 0x54 and writes[#writes - 1].value == 70)
        assert(writes[#writes].index == 0x55 and writes[#writes].value == 70)
    end
end
function tests.cleanup_errors_propagate_and_all_attempted()
    local f = firmware(); read(f, 1, 68); ok(f:run(1, "Write")); local before = #f:writes()
    f.failures[0x50] = "first failure"; f.failures[0x54] = "second failure"
    local e = f:run(1, "Reset")
    assert(e == "first failure" and #f:writes() == before + 3 and f.targets[0x55] == 70)
end
function tests.nonzero_write_response_is_error()
    local f = firmware(); read(f, 1, 68); f.write_response = 1
    fails(f:run(1, "Write")); assert(#f:writes() == 3)
end

-- Remove every general-purpose host escape before executing any handler.
os = nil; require = nil; dofile = nil; loadfile = nil; package = nil; debug = nil
assert(tests[scenario], "unknown scenario")()
print("PASS " .. scenario)
