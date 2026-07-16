// voip_crypto.cpp — OpenSSL crypto glue for libtgvoip (telegramdesktop fork).
// This fork ships no built-in crypto: the embedder must fill
// tgvoip::VoIPController::crypto. Implementations lifted from grishka/libtgvoip.
#include <openssl/aes.h>
#include <openssl/modes.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include "VoIPController.h"

namespace {

void tg_rand_bytes(uint8_t* buffer, size_t len) {
    RAND_bytes(buffer, static_cast<int>(len));
}
void tg_sha1(uint8_t* msg, size_t len, uint8_t* output) {
    SHA1(msg, len, output);
}
void tg_sha256(uint8_t* msg, size_t len, uint8_t* output) {
    SHA256(msg, len, output);
}
void tg_aes_ige_encrypt(uint8_t* in, uint8_t* out, size_t length, uint8_t* key, uint8_t* iv) {
    AES_KEY akey;
    AES_set_encrypt_key(key, 32 * 8, &akey);
    AES_ige_encrypt(in, out, length, &akey, iv, AES_ENCRYPT);
}
void tg_aes_ige_decrypt(uint8_t* in, uint8_t* out, size_t length, uint8_t* key, uint8_t* iv) {
    AES_KEY akey;
    AES_set_decrypt_key(key, 32 * 8, &akey);
    AES_ige_encrypt(in, out, length, &akey, iv, AES_DECRYPT);
}
void tg_aes_ctr_encrypt(uint8_t* inout, size_t length, uint8_t* key, uint8_t* iv,
                        uint8_t* ecount, uint32_t* num) {
    AES_KEY akey;
    AES_set_encrypt_key(key, 32 * 8, &akey);
    CRYPTO_ctr128_encrypt(inout, inout, length, &akey, iv, ecount, num,
                          (block128_f)AES_encrypt);
}
void tg_aes_cbc_encrypt(uint8_t* in, uint8_t* out, size_t length, uint8_t* key, uint8_t* iv) {
    AES_KEY akey;
    AES_set_encrypt_key(key, 256, &akey);
    AES_cbc_encrypt(in, out, length, &akey, iv, AES_ENCRYPT);
}
void tg_aes_cbc_decrypt(uint8_t* in, uint8_t* out, size_t length, uint8_t* key, uint8_t* iv) {
    AES_KEY akey;
    AES_set_decrypt_key(key, 256, &akey);
    AES_cbc_encrypt(in, out, length, &akey, iv, AES_DECRYPT);
}

}  // namespace

// Definition of the static member `crypto` declared in VoIPController.h.
// Type is the top-level tgvoip::CryptoFunctions (not nested in VoIPController).
tgvoip::CryptoFunctions tgvoip::VoIPController::crypto = {
    tg_rand_bytes,
    tg_sha1,
    tg_sha256,
    tg_aes_ige_encrypt,
    tg_aes_ige_decrypt,
    tg_aes_ctr_encrypt,
    tg_aes_cbc_encrypt,
    tg_aes_cbc_decrypt,
};
