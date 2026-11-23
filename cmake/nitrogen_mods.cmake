# Original trusted mod plugins. The pinned framework already scans
# <exe-dir>/mods, commits the selected plan and activates registered plugins on
# every launch. Linking a plugin only registers its id; a package feature must
# still select it. No game data, generated code or framework source is added or
# changed here.
#
# Like Nitrogen's other integrations, plugins are linked only when the
# framework's mod runtime is part of the target. Player plugins are then linked
# into every build; development-only plugins also need NITROGEN_MODS=ON.
set(_nitrogen_player_mod_sources
    "${CMAKE_CURRENT_SOURCE_DIR}/native/mods/soundtrack.cpp")
set(_nitrogen_player_mod_ids "nitrogen.soundtrack")
set(_nitrogen_development_mod_sources
    "${CMAKE_CURRENT_SOURCE_DIR}/native/mods/mod_probe.cpp")
set(_nitrogen_development_mod_ids "nitrogen.mod-probe")

if("${PSXRECOMP_ROOT}/runtime/src/mod_runtime.cpp" IN_LIST _nitrogen_sources)
    target_sources(nitrogen-runtime PRIVATE ${_nitrogen_player_mod_sources})
    if(NITROGEN_MODS)
        target_sources(nitrogen-runtime PRIVATE ${_nitrogen_development_mod_sources})
        message(STATUS "Nitrogen trusted mod plugins: ${_nitrogen_player_mod_ids};${_nitrogen_development_mod_ids}")
    else()
        message(STATUS "Nitrogen trusted mod plugins: ${_nitrogen_player_mod_ids}")
    endif()
elseif(NITROGEN_MODS)
    message(FATAL_ERROR "NITROGEN_MODS needs the framework mod runtime (runtime/src/mod_runtime.cpp)")
else()
    message(STATUS "Nitrogen trusted mod plugins: none (framework mod runtime not in this target)")
endif()
