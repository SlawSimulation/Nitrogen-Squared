# Original trusted mod plugins, linked only when NITROGEN_MODS is ON.
# The pinned framework already scans <exe-dir>/mods, commits the selected plan
# and activates registered plugins on every launch. Linking a plugin only
# registers its id; a package feature must still select it. No game data,
# generated code or framework source is added or changed here.
set(_nitrogen_mod_sources
    "${CMAKE_CURRENT_SOURCE_DIR}/native/mods/mod_probe.cpp")
target_sources(nitrogen-runtime PRIVATE ${_nitrogen_mod_sources})
message(STATUS "Nitrogen trusted mod plugins: nitrogen.mod-probe")
