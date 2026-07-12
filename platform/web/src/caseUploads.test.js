import { describe, expect, it, vi } from "vitest";
import { uploadCaseZip } from "./caseUploads.js";

describe("routine case ZIP upload", () => {
  it("hashes, chunks, and completes a durable upload", async () => {
    const file = new File([new Uint8Array([1, 2, 3, 4, 5])], "study.zip");
    const client = {
      createCaseUpload: vi.fn().mockResolvedValue({
        id: "upload-1", received_size: 0, total_size: 5, max_chunk_size: 2,
      }),
      uploadCaseChunk: vi.fn()
        .mockResolvedValueOnce({ received_size: 2 })
        .mockResolvedValueOnce({ received_size: 4 })
        .mockResolvedValueOnce({ received_size: 5 }),
      completeCaseUpload: vi.fn().mockResolvedValue({ id: "upload-1", status: "staged" }),
    };
    const progress = [];
    const result = await uploadCaseZip("HMRI-001", file, {
      client, chunkSize: 2, onProgress: (value) => progress.push(value), resumeStore: null,
    });
    expect(result.status).toBe("staged");
    expect(client.createCaseUpload).toHaveBeenCalledWith(expect.objectContaining({
      pseudonym: "HMRI-001", filename: "study.zip", total_size: 5,
    }));
    expect(client.uploadCaseChunk).toHaveBeenCalledTimes(3);
    expect(progress.some((item) => item.phase === "verifying")).toBe(true);
  });

  it("rejects non-ZIP input before creating a server session", async () => {
    await expect(uploadCaseZip("HMRI-001", new File(["x"], "scan.dcm"), {
      client: {}, resumeStore: null,
    })).rejects.toThrow(/ZIP/i);
  });
});
