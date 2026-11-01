/*
 * Stand-in for Steam's steamwebhelper.exe under Wine on macOS.
 *
 * Steam's interface is a Chromium (CEF) browser. Under Wine on macOS its
 * windows stay black unless it runs as one process with the GPU off, and
 * Steam gives no option for that. This program takes the helper's place,
 * starts the real one (steamwebhelper_real.exe, next to it) with Steam's
 * arguments followed by those flags, and returns its exit code.
 *
 * The approach comes from notpop/steam-on-m1-wine and
 * ramiabih/play-windows-steam-on-mac. Installed by portkit/steam.py, which
 * puts it back after Steam updates replace it.
 */

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define REAL L"steamwebhelper_real.exe"
/* After Steam's own flags, so these win (site isolation needs several processes). */
#define FLAGS L"--disable-gpu --single-process " \
              L"--disable-features=IsolateOrigins,site-per-process,SpareRendererForSitePerProcess"

/* The command line without its first token (this program's own path). */
static const wchar_t *arguments(void)
{
    const wchar_t *p = GetCommandLineW();
    int quoted = 0;
    for (; *p; p++) {
        if (*p == L'"') quoted = !quoted;
        else if (*p == L' ' && !quoted) break;
    }
    while (*p == L' ') p++;
    return p;
}

int wmain(void)
{
    wchar_t real[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, real, MAX_PATH);
    wchar_t *slash = n && n < MAX_PATH ? wcsrchr(real, L'\\') : NULL;
    if (!slash || (size_t)(slash + 1 - real) + wcslen(REAL) >= MAX_PATH) return 1;
    wcscpy(slash + 1, REAL);

    const wchar_t *args = arguments();
    size_t size = wcslen(real) + wcslen(args) + wcslen(FLAGS) + 8;
    wchar_t *cmdline = calloc(size, sizeof(wchar_t));
    if (!cmdline) return 1;
    _snwprintf(cmdline, size, L"\"%ls\" %ls %ls", real, args, FLAGS);

    STARTUPINFOW si = { .cb = sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(real, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) return 1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
