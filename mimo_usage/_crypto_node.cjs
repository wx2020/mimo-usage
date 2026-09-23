#!/usr/bin/env node
// MiMo 密码登录参数加密（复刻 account.xiaomi.com crypto.17efe504 模块 encryptAes）
// stdin:  {"user": "<手机号/用户名>"}          （明文，仅进程管道内）
// stdout: {"user": "<AES-CBC base64>", "eui": "<RSA( aesKey ).dXNlcg=="}
// 算法：随机16字符 AES-128 key + IV="0102030405060708"(UTF-8) + PKCS7，
//       key 经 RSA-1024-PKCS1v1.5(硬编码公钥) 加密；EUI = rsa + "." + btoa("user")
"use strict";
const crypto = require("crypto");

const PUB_KEY = `-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCYEVrK/4Mahiv0pUJgTybx4J9P5
dUT/Y0PuwMbk+gMU+jrZnBiXGv6/hCH1avIhoBcE535F8nJQQN3UavZdFkYidsoXu
Enat3+eVTp3FslyhRwIBDF09v4vDhRtxFOT+R7uH7h/mzmyA2/+lfIMWGIrffXprY
izbV76+YQKhoqFQIDAQAB
-----END PUBLIC KEY-----`;

const CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*";
const IV = Buffer.from("0102030405060708", "utf8");

function randomKey16() {
  let out = "";
  for (let i = 0; i < 16; i++) out += CHARSET[crypto.randomInt(CHARSET.length)];
  return out;
}

function encryptAes(params) {
  const aesKey = randomKey16();
  const rsaB64 = crypto
    .publicEncrypt(
      { key: PUB_KEY, padding: crypto.constants.RSA_PKCS1_PADDING },
      Buffer.from(Buffer.from(aesKey, "utf8").toString("base64"), "utf8")
    )
    .toString("base64");
  const fieldsB64 = Buffer.from(Object.keys(params).join(","), "utf8").toString("base64");
  const key = Buffer.from(aesKey, "utf8");
  const encrypted = {};
  for (const [name, value] of Object.entries(params)) {
    const cipher = crypto.createCipheriv("aes-128-cbc", key, IV);
    encrypted[name] = Buffer.concat([cipher.update(String(value), "utf8"), cipher.final()]).toString("base64");
  }
  return { EUI: `${rsaB64}.${fieldsB64}`, encryptedParams: encrypted };
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (raw += chunk));
process.stdin.on("end", () => {
  try {
    const input = JSON.parse(raw || "{}");
    if (!input.user) throw new Error("missing user");
    const { EUI, encryptedParams } = encryptAes({ user: input.user });
    process.stdout.write(JSON.stringify({ user: encryptedParams.user, eui: EUI }));
  } catch (err) {
    process.stderr.write(String(err && err.message ? err.message : err));
    process.exit(1);
  }
});
