// SPDX-License-Identifier: GPL-3.0-or-later
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
if (import.meta.env.DEV && !window.__TAURI_INTERNALS__) await import('./dev/mock.js');
export const native = (cmd,args) => invoke(cmd,args);
export const on = (event,fn) => listen(event,e=>fn(e.payload));
// Native file pickers run in the host; only their selection reaches the allowlisted service.
const pickers={choose:'choose_disc',choose_mod:'choose_mod',choose_wav:'choose_wav'};
export const request = (command,args={}) => pickers[command] ? invoke(pickers[command],args) : invoke('request',{command,args});
