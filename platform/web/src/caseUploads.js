import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { api } from "./api.js";

export const CASE_UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024;

async function hashFile(file, chunkSize, onProgress) {
  const digest = sha256.create();
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const end = Math.min(offset + chunkSize, file.size);
    digest.update(new Uint8Array(await file.slice(offset, end).arrayBuffer()));
    onProgress({ phase: "hashing", loaded: end, total: file.size });
  }
  return bytesToHex(digest.digest());
}

export async function uploadCaseZip(pseudonym, file, {
  client = api,
  chunkSize = CASE_UPLOAD_CHUNK_SIZE,
  onProgress = () => {},
  resumeStore,
} = {}) {
  if (!file || !file.name?.toLowerCase().endsWith(".zip")) {
    throw new Error("Select one DICOM ZIP archive");
  }
  if (resumeStore === undefined) {
    try { resumeStore = globalThis.sessionStorage; } catch { resumeStore = null; }
  }
  const checksum = await hashFile(file, chunkSize, onProgress);
  const resumeKey = `meld7t:case-upload:${pseudonym}:${checksum}:${file.size}`;
  let upload = null;
  try {
    const saved = JSON.parse(resumeStore?.getItem(resumeKey) || "null");
    if (saved?.id) {
      const current = await client.getCaseUpload(saved.id);
      if (current?.status === "receiving" && current.total_size === file.size) upload = current;
      else if (["staged", "importing", "ready"].includes(current?.status)) return current;
    }
  } catch { /* stale/inaccessible browser state starts a new durable session */ }
  if (!upload) {
    upload = await client.createCaseUpload({
      pseudonym,
      filename: file.name,
      total_size: file.size,
      sha256: checksum,
      content_type: "application/zip",
    });
  }
  const uploadId = upload.id;
  let offset = Number(upload.received_size || 0);
  if (!uploadId || !Number.isSafeInteger(offset) || offset < 0 || offset > file.size) {
    throw new Error("The upload service returned an invalid resumable session");
  }
  const accepted = Math.min(chunkSize, Number(upload.max_chunk_size) || chunkSize);
  try { resumeStore?.setItem(resumeKey, JSON.stringify({ id: uploadId })); } catch { /* optional */ }
  while (offset < file.size) {
    const end = Math.min(offset + accepted, file.size);
    const result = await client.uploadCaseChunk(
      uploadId, offset, await file.slice(offset, end).arrayBuffer());
    const next = Number(result?.received_size ?? end);
    if (!Number.isSafeInteger(next) || next <= offset || next > file.size) {
      throw new Error("The upload service did not advance the durable offset");
    }
    offset = next;
    onProgress({ phase: "uploading", loaded: offset, total: file.size });
  }
  onProgress({ phase: "verifying", loaded: file.size, total: file.size });
  const completed = await client.completeCaseUpload(uploadId);
  try { resumeStore?.removeItem(resumeKey); } catch { /* server completion is authoritative */ }
  return completed;
}
