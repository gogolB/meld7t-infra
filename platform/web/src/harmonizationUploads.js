import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { api } from "./api.js";

export const DEFAULT_UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024;

export async function sha256File(file, chunkSize = DEFAULT_UPLOAD_CHUNK_SIZE) {
  const digest = sha256.create();
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const part = file.slice(offset, Math.min(offset + chunkSize, file.size));
    digest.update(new Uint8Array(await part.arrayBuffer()));
  }
  return bytesToHex(digest.digest());
}

export async function uploadCohortFiles(cohortId, files, {
  client = api,
  chunkSize = DEFAULT_UPLOAD_CHUNK_SIZE,
  onProgress = () => {},
  resumeStore,
} = {}) {
  if (resumeStore === undefined) {
    try { resumeStore = globalThis.sessionStorage; } catch { resumeStore = null; }
  }
  const completed = [];
  const fileList = Array.from(files || []);
  for (let fileIndex = 0; fileIndex < fileList.length; fileIndex += 1) {
    const file = fileList[fileIndex];
    onProgress({ phase: "hashing", file: file.name, fileIndex, fileCount: fileList.length,
      loaded: 0, total: file.size });
    const checksum = await sha256File(file, chunkSize);
    const resumeKey = `meld7t:harmonization-upload:${cohortId}:${checksum}:${file.size}`;
    let upload = null;
    try {
      const saved = JSON.parse(resumeStore?.getItem(resumeKey) || "null");
      if (saved?.id && Number.isSafeInteger(saved.received_size)) {
        const current = await client.getHarmonizationUpload(cohortId, saved.id);
        if (current?.status === "receiving" && current.total_size === file.size) upload = current;
      }
    } catch { /* inaccessible or stale browser state starts a fresh upload */ }
    if (!upload) {
      upload = await client.createHarmonizationUpload(cohortId, {
        filename: file.name,
        total_size: file.size,
        sha256: checksum,
      });
    }
    const uploadId = upload?.id || upload?.upload_id;
    if (!uploadId) throw new Error("upload session did not return an id");
    let offset = Number(upload.offset || upload.next_offset || upload.received_size || 0);
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > file.size) {
      throw new Error(`upload session returned an invalid offset for ${file.name}`);
    }
    try { resumeStore?.setItem(resumeKey, JSON.stringify({
      id: uploadId, received_size: offset, max_chunk_size: upload.max_chunk_size,
    })); } catch { /* resumability is best-effort when browser storage is disabled */ }
    const acceptedChunkSize = Math.min(chunkSize, Number(upload.max_chunk_size) || chunkSize);
    while (offset < file.size) {
      const end = Math.min(offset + acceptedChunkSize, file.size);
      const chunk = await file.slice(offset, end).arrayBuffer();
      const result = await client.uploadHarmonizationChunk(
        cohortId, uploadId, offset, chunk);
      const next = Number(result?.offset ?? result?.next_offset ?? result?.received_size ?? end);
      if (!Number.isSafeInteger(next) || next <= offset || next > file.size) {
        throw new Error(`upload did not advance for ${file.name}`);
      }
      offset = next;
      try { resumeStore?.setItem(resumeKey, JSON.stringify({
        id: uploadId, received_size: offset, max_chunk_size: acceptedChunkSize,
      })); } catch { /* upload remains valid even when progress cannot be persisted */ }
      onProgress({ phase: "uploading", file: file.name, fileIndex,
        fileCount: fileList.length, loaded: offset, total: file.size });
    }
    onProgress({ phase: "verifying", file: file.name, fileIndex,
      fileCount: fileList.length, loaded: file.size, total: file.size });
    completed.push(await client.completeHarmonizationUpload(cohortId, uploadId));
    try { resumeStore?.removeItem(resumeKey); } catch { /* completion is authoritative */ }
  }
  return completed;
}
