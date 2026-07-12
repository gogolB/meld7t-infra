import { describe, expect, it, vi } from "vitest";
import { sha256File, uploadCohortFiles } from "./harmonizationUploads.js";

function memoryFile(name, text) {
  const bytes = new TextEncoder().encode(text);
  return {
    name,
    size: bytes.length,
    slice(start, end) {
      const part = bytes.slice(start, end);
      return { arrayBuffer: async () => part.buffer };
    },
  };
}

describe("harmonization uploads", () => {
  it("calculates SHA-256 incrementally", async () => {
    expect(await sha256File(memoryFile("study.dcm", "abc"), 2)).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("resumes at the server offset and verifies completion", async () => {
    const client = {
      createHarmonizationUpload: vi.fn().mockResolvedValue({
        id: "upload-1", received_size: 2, max_chunk_size: 2,
      }),
      uploadHarmonizationChunk: vi.fn().mockResolvedValue(null),
      completeHarmonizationUpload: vi.fn().mockResolvedValue({ imported: true }),
    };
    const progress = vi.fn();
    await uploadCohortFiles("cohort-1", [memoryFile("study.dcm", "abcdef")], {
      client, chunkSize: 2, onProgress: progress,
    });
    expect(client.createHarmonizationUpload).toHaveBeenCalledWith("cohort-1", {
      filename: "study.dcm", total_size: 6,
      sha256: "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721",
    });
    expect(client.uploadHarmonizationChunk.mock.calls[0].slice(0, 3)).toEqual(
      ["cohort-1", "upload-1", 2]);
    expect(client.uploadHarmonizationChunk.mock.calls[0][3].byteLength).toBe(2);
    expect(client.uploadHarmonizationChunk.mock.calls[1].slice(0, 3)).toEqual(
      ["cohort-1", "upload-1", 4]);
    expect(client.uploadHarmonizationChunk.mock.calls[1][3].byteLength).toBe(2);
    expect(client.completeHarmonizationUpload).toHaveBeenCalledWith("cohort-1", "upload-1");
    expect(progress).toHaveBeenLastCalledWith(expect.objectContaining({
      phase: "verifying", loaded: 6, total: 6,
    }));
  });

  it("resumes a checksum-matched upload after a page reload", async () => {
    const file = memoryFile("study.dcm", "abcdef");
    const checksum = "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721";
    const key = `meld7t:harmonization-upload:cohort-1:${checksum}:6`;
    const entries = new Map([[key, JSON.stringify({
      id: "upload-existing", received_size: 4, max_chunk_size: 2,
    })]]);
    const resumeStore = {
      getItem: vi.fn((name) => entries.get(name) || null),
      setItem: vi.fn((name, value) => entries.set(name, value)),
      removeItem: vi.fn((name) => entries.delete(name)),
    };
    const client = {
      createHarmonizationUpload: vi.fn(),
      getHarmonizationUpload: vi.fn().mockResolvedValue({
        id: "upload-existing", status: "receiving", received_size: 4,
        total_size: 6, max_chunk_size: 2,
      }),
      uploadHarmonizationChunk: vi.fn().mockResolvedValue({ received_size: 6 }),
      completeHarmonizationUpload: vi.fn().mockResolvedValue({ status: "staged" }),
    };
    await uploadCohortFiles("cohort-1", [file], { client, chunkSize: 2, resumeStore });
    expect(client.createHarmonizationUpload).not.toHaveBeenCalled();
    expect(client.getHarmonizationUpload).toHaveBeenCalledWith(
      "cohort-1", "upload-existing");
    expect(client.uploadHarmonizationChunk.mock.calls[0].slice(0, 3)).toEqual(
      ["cohort-1", "upload-existing", 4]);
    expect(resumeStore.removeItem).toHaveBeenCalledWith(key);
  });
});
