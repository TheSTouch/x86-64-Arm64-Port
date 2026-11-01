// steam-signin: signs this Mac in to Steam as a Steam client, through Kettle, for the Kettle
// launch mode (./port steam signin). The game's steam_api64.dll then logs on with the refresh
// token this leaves, and asks Steam to sign the game's ownership ticket.
//
//   steam-signin                QR code in the terminal: scan it in the Steam Mobile app
//                               (Steam Guard, the QR scanner), then approve
//   steam-signin <account>      account name, then the password (not echoed); Steam Guard
//                               asks for a code or an approval in the Steam Mobile app
//
// The token is kept the way Kettle keeps it on macOS (RecompSteamAuth_StoreToken): the Keychain
// item "Recomp Steam" / "steam-client-token", one for every game on this Mac, the same one
// Mac To iOS Recomp's recomp-steam-cli uses. Exit codes: 0 signed in, 1 failed.
#import <Foundation/Foundation.h>
#import <CoreImage/CoreImage.h>
#include <readpassphrase.h>
#include <stdio.h>
#include "RecompSteamAuth.h"
#include "RecompSteamSession.h"

static void Out(const char *format, ...) __attribute__((format(printf, 1, 2)));
static void Out(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    vprintf(format, ap);
    va_end(ap);
    printf("\n");
    fflush(stdout);
}

// The QR code with two modules per character cell (upper and lower half blocks), black on
// white whatever the terminal's colours, with the quiet zone the scanner needs.
static void PrintQR(const char *url)
{
    CIFilter *f = [CIFilter filterWithName:@"CIQRCodeGenerator"];
    [f setValue:[@(url) dataUsingEncoding:NSUTF8StringEncoding] forKey:@"inputMessage"];
    [f setValue:@"L" forKey:@"inputCorrectionLevel"];
    CIImage *img = f.outputImage;   // one pixel per module
    CIContext *ctx = [CIContext contextWithOptions:nil];
    CGImageRef cg = [ctx createCGImage:img fromRect:img.extent];
    size_t w = CGImageGetWidth(cg), h = CGImageGetHeight(cg);
    uint8_t *gray = calloc(w * h, 1);
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceGray();
    CGContextRef bc = CGBitmapContextCreate(gray, w, h, 8, w, cs, (CGBitmapInfo)kCGImageAlphaNone);
    CGContextDrawImage(bc, CGRectMake(0, 0, w, h), cg);
    const int quiet = 2;
    int size = (int)w + 2 * quiet;
    #define DARK(x, y) ((x) >= quiet && (y) >= quiet && (x) - quiet < (int)w && (y) - quiet < (int)h && \
                        gray[((y) - quiet) * w + (x) - quiet] < 128)
    for (int y = 0; y < size; y += 2) {
        printf("\033[30;47m");
        for (int x = 0; x < size; x++) {
            bool top = DARK(x, y), bottom = y + 1 < size && DARK(x, y + 1);
            printf("%s", top && bottom ? "█" : top ? "▀" : bottom ? "▄" : " ");
        }
        printf("\033[0m\n");
    }
    #undef DARK
    fflush(stdout);
    CGContextRelease(bc);
    CGColorSpaceRelease(cs);
    CGImageRelease(cg);
    free(gray);
}

static NSString *DeviceName(void)
{
    NSString *host = NSProcessInfo.processInfo.hostName;
    if ([host hasSuffix:@".local"]) host = [host substringToIndex:host.length - 6];
    return [NSString stringWithFormat:@"Porting Kit on %@", host];
}

static void Done(int32_t er, const char *refresh, const char *account)
{
    if (er != RecompSteamResultOK || !refresh) {
        Out("Sign-in failed (Steam EResult %d).", er);
        exit(1);
    }
    if (!RecompSteamAuth_IsClientToken(refresh)) {
        Out("Steam did not issue a Steam client sign-in; nothing stored.");
        exit(1);
    }
    if (!RecompSteamAuth_StoreToken(refresh)) {
        Out("Signed in, but the Keychain refused the sign-in.");
        exit(1);
    }
    Out("Signed in as %s (SteamID %llu). Kept in the Keychain (\"Recomp Steam\"); Steam lists it as \"%s\".",
        account ? account : "?", RecompSteamAuth_TokenSteamID(refresh), DeviceName().UTF8String);
    exit(0);
}

static void SignInQR(void)
{
    __block int shown = 0;
    RecompSteamAuth_BeginQR(DeviceName().UTF8String, 600, ^(const char *url) {
        Out(shown++ ? "\nThe code was renewed:" : "Scan this in the Steam Mobile app (Steam Guard, the QR scanner):");
        PrintQR(url);
    }, ^{
        Out("Scanned: approve the sign-in in the Steam Mobile app.");
    }, ^(int32_t er, const char *refresh, const char *access, const char *account) {
        (void)access;
        Done(er, refresh, account);
    });
}

static int gFlow;

static void SignInPassword(const char *account)
{
    char password[256];
    if (!readpassphrase("Steam password: ", password, sizeof password, RPP_REQUIRE_TTY)) {
        Out("No password read.");
        exit(1);
    }
    gFlow = RecompSteamAuth_BeginPassword(DeviceName().UTF8String, account, password,
                                          ^(const int *types, int count, const char *email_domain) {
        int code_type = 0;
        for (int i = 0; i < count; i++) {
            if (types[i] == RecompSteamGuardDeviceCode) code_type = types[i];
            else if (types[i] == RecompSteamGuardEmailCode && !code_type) code_type = types[i];
        }
        for (int i = 0; i < count; i++)
            if (types[i] == RecompSteamGuardDeviceConfirmation)
                Out("Approve the sign-in in the Steam Mobile app%s.", code_type ? ", or type the Steam Guard code" : "");
        if (!code_type) return;
        // The code is read off the session queue, so the confirmation polling goes on meanwhile.
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            char code[32];
            const char *prompt = code_type == RecompSteamGuardEmailCode ? "Code from the email to " : "Steam Guard code: ";
            if (code_type == RecompSteamGuardEmailCode) printf("%s%s: ", prompt, email_domain ? email_domain : "you");
            else printf("%s", prompt);
            fflush(stdout);
            if (!fgets(code, sizeof code, stdin)) return;
            code[strcspn(code, "\r\n")] = 0;
            RecompSteamAuth_SubmitCode(gFlow, code, code_type, ^(int32_t er) {
                if (er != RecompSteamResultOK) Out("Steam refused the code (EResult %d).", er);
            });
        });
    }, ^(int32_t er, const char *refresh, const char *access, const char *name) {
        (void)access;
        Done(er, refresh, name ? name : account);
    });
    memset(password, 0, sizeof password);
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc > 2 || (argc == 2 && argv[1][0] == '-')) {
            fprintf(stderr, "usage: steam-signin [account name]\n");
            return 2;
        }
        if (argc == 2) SignInPassword(argv[1]);
        else SignInQR();
    }
    dispatch_main();
}
