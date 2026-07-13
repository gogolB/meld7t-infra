import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getHarmonizationCohort: vi.fn(),
  getHarmonizationBuild: vi.fn(),
  getHarmonizationBuildQc: vi.fn(),
  importHarmonizationStudies: vi.fn(),
  submitHarmonizationDemographics: vi.fn(),
  freezeHarmonizationCohort: vi.fn(),
  createHarmonizationBuild: vi.fn(),
  cancelHarmonizationBuild: vi.fn(),
  decideHarmonizationStudy: vi.fn(),
  validateHarmonizationBuild: vi.fn(),
  activateHarmonizationBuild: vi.fn(),
  createHarmonizationUpload: vi.fn(),
  uploadHarmonizationChunk: vi.fn(),
  completeHarmonizationUpload: vi.fn(),
  getHarmonizationUploadRollbackEvidence: vi.fn(),
  resolveHarmonizationUploadRollback: vi.fn(),
}));

vi.mock("../api.js", () => ({ api: mocks }));

import HarmonizationCohort from "./HarmonizationCohort.jsx";

const draftCohort = {
  id: "cohort-1", name: "Example cohort", site_code: "SITE_A", profile_code: "H_SITE_A",
  profile_version: 1, status: "draft", source_role: "t1_uni", min_controls: 20,
  cv_folds: 5, selector: { roles: ["t1_uni"], acquisition: { model: "Terra" } },
  counts: { studies: 20, included: 20, excluded: 0, demographics: 20 }, builds: [],
  studies: [{ id: "study-1", subject_key_hmac: "d".repeat(64), study_uid: "1.2.840.1",
    acquisition_fingerprint: "a".repeat(64), included: true }],
};

function renderPage() {
  return render(<MemoryRouter initialEntries={["/admin/harmonization/cohorts/cohort-1"]}>
    <Routes><Route path="/admin/harmonization/cohorts/:id"
      element={<HarmonizationCohort />} /></Routes>
  </MemoryRouter>);
}

describe("HarmonizationCohort", () => {
  beforeEach(() => {
    mocks.getHarmonizationCohort.mockResolvedValue(draftCohort);
    mocks.getHarmonizationBuild.mockResolvedValue(null);
    mocks.getHarmonizationBuildQc.mockResolvedValue(null);
    mocks.importHarmonizationStudies.mockResolvedValue({ imported: 1 });
    mocks.submitHarmonizationDemographics.mockResolvedValue({ valid: true });
    mocks.freezeHarmonizationCohort.mockResolvedValue({ status: "frozen" });
    mocks.decideHarmonizationStudy.mockResolvedValue({ included: false });
    mocks.createHarmonizationBuild.mockResolvedValue({ id: "build-1", status: "queued" });
    mocks.cancelHarmonizationBuild.mockResolvedValue({ status: "cancelled" });
    mocks.validateHarmonizationBuild.mockResolvedValue({ status: "validated" });
    mocks.activateHarmonizationBuild.mockResolvedValue({ status: "active" });
    mocks.getHarmonizationUploadRollbackEvidence.mockResolvedValue({
      schema_version: 1,
      receipt_evidence_sha256: "f".repeat(64),
      pending_counts: { ambiguous_instances: 1, owned_delete_failures: 0 },
      instances: [{ sop_instance_uid: "1.2.840.9", worker_owned: false }],
    });
    mocks.resolveHarmonizationUploadRollback.mockResolvedValue({ status: "failed" });
  });

  it("imports selected Orthanc studies and submits demographics CSV", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("Example cohort")).toBeInTheDocument();
    await user.type(screen.getByLabelText("One StudyInstanceUID,subject_key pair per line"),
      "1.2.840.2,HC-002");
    await user.click(screen.getByRole("button", { name: "Import selected studies" }));
    await waitFor(() => expect(mocks.importHarmonizationStudies).toHaveBeenCalledWith(
      "cohort-1", [{ study_uid: "1.2.840.2", subject_key: "HC-002", included: true }],
    ));

    const csv = new File(["ID,Age,Sex\nHC-001,31,F\n"], "demographics.csv",
      { type: "text/csv" });
    Object.defineProperty(csv, "text", { value: vi.fn().mockResolvedValue(
      "ID,Age,Sex\nHC-001,31,F\n") });
    await user.upload(screen.getByLabelText("CSV with exact ID,Age,Sex headers"), csv);
    await user.click(screen.getByRole("button", { name: "Validate demographics" }));
    await waitFor(() => expect(mocks.submitHarmonizationDemographics).toHaveBeenCalledWith(
      "cohort-1", "ID,Age,Sex\nHC-001,31,F\n"));
  });

  it("freezes only after the minimum eligible cohort is present", async () => {
    const user = userEvent.setup();
    renderPage();
    const button = await screen.findByRole("button", { name: "Freeze cohort" });
    expect(button).toBeEnabled();
    await user.click(button);
    await waitFor(() => expect(mocks.freezeHarmonizationCohort).toHaveBeenCalledWith("cohort-1"));
  });

  it("records an explicit reason when excluding a control", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Exclude" }));
    await user.type(screen.getByLabelText("Exclusion reason *"), "Acquisition protocol outlier");
    await user.click(screen.getByRole("button", { name: "Save exclusion" }));
    await waitFor(() => expect(mocks.decideHarmonizationStudy).toHaveBeenCalledWith(
      "cohort-1", "study-1", false, "Acquisition protocol outlier"));
  });

  it("queues a build with operator-supplied image and acceptance policy", async () => {
    mocks.getHarmonizationCohort.mockResolvedValue({ ...draftCohort, status: "frozen" });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("2. Start harmonization build");
    await user.type(screen.getByLabelText("Approved builder image digest *"),
      `localhost/meld-builder@sha256:${"b".repeat(64)}`);
    fireEvent.change(screen.getByLabelText("Versioned acceptance criteria (JSON) *"), {
      target: { value: `{"policy_id":"MELD-SITE-QC-v1","methodology_sha256":"${"a".repeat(64)}","required_metrics":{"residual_site_effect":{"max":0.1}}}` },
    });
    await user.click(screen.getByRole("button", { name: "Queue build" }));
    await waitFor(() => expect(mocks.createHarmonizationBuild).toHaveBeenCalledWith("cohort-1", {
      acceptance_criteria: { policy_id: "MELD-SITE-QC-v1", methodology_sha256: "a".repeat(64),
        required_metrics: { residual_site_effect: { max: 0.1 } } },
      builder_image_digest: `localhost/meld-builder@sha256:${"b".repeat(64)}`,
    }));
  });

  it("requires the full external scientific validation report at QC review", async () => {
    const report = {
      schema_version: 1,
      profile: { code: "H_SITE_A", version: 1, detector_id: "meld_fcd" },
      approval_id: "IRB-SCI-42",
      independent_reviewer: "admin-2@example.org",
      approved_at: "2026-07-11T12:00:00Z",
      acquisition_fingerprints: ["a".repeat(64)],
      qc: { included: 20, excluded: 0 },
      holdout: { case_count: 6, positive_cases: 2, negative_cases: 2, control_cases: 2 },
      metrics_sha256: "b".repeat(64),
      golden_case_evidence_sha256: "c".repeat(64),
      methodology_sha256: "d".repeat(64),
      image_digests: { builder: `localhost/meld-builder@sha256:${"e".repeat(64)}` },
      builder_adapter_sha256: "f".repeat(64),
    };
    mocks.getHarmonizationCohort.mockResolvedValue({
      ...draftCohort, status: "qc_review", latest_build_id: "build-1",
      builds: [{ id: "build-1", status: "qc_review" }],
    });
    mocks.getHarmonizationBuild.mockResolvedValue({
      id: "build-1", status: "qc_review", stage: "qc", progress: 1,
      builder_adapter_sha256: "f".repeat(64),
    });
    mocks.getHarmonizationBuildQc.mockResolvedValue({ fold_count: 5, passed: true });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/internal control-cohort cross-validation/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Scientific validation report *"), {
      target: { value: JSON.stringify(report) },
    });
    await user.click(screen.getByRole("button", { name: "Validate candidate" }));
    await waitFor(() => expect(mocks.validateHarmonizationBuild).toHaveBeenCalledWith(
      "build-1", report));
  });

  it("supports cancellation and single-admin activation lifecycle actions", async () => {
    mocks.getHarmonizationCohort.mockResolvedValue({
      ...draftCohort, status: "frozen", latest_build_id: "build-1",
      builds: [{ id: "build-1", status: "queued" }],
    });
    mocks.getHarmonizationBuild.mockResolvedValue({
      id: "build-1", status: "queued", stage: "waiting", progress: 0,
    });
    const user = userEvent.setup();
    const first = renderPage();
    await user.click(await screen.findByRole("button", { name: "Cancel build" }));
    await waitFor(() => expect(mocks.cancelHarmonizationBuild).toHaveBeenCalledWith("build-1"));
    first.unmount();

    mocks.getHarmonizationCohort.mockResolvedValue({
      ...draftCohort, status: "frozen", latest_build_id: "build-2",
      builds: [{ id: "build-2", status: "validated" }],
    });
    mocks.getHarmonizationBuild.mockResolvedValue({
      id: "build-2", status: "validated", stage: "validated", progress: 1,
    });
    mocks.getHarmonizationBuildQc.mockResolvedValue({ folds: 5, all_folds_succeeded: true });
    renderPage();
    expect(await screen.findByText(/authenticated actor and evidence/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate profile" }));
    await waitFor(() => expect(mocks.activateHarmonizationBuild).toHaveBeenCalledWith("build-2"));
  });

  it("verifies canonical receipt evidence before approving exact rollback deletion", async () => {
    mocks.getHarmonizationCohort.mockResolvedValue({
      ...draftCohort,
      uploads: [{
        id: "upload-1", filename: "controls.zip", status: "failed",
        received_size: 2048, total_size: 2048, last_error: "rollback_pending",
        import_result: {
          phase: "rollback_incomplete", ambiguous_instances: 1,
          receipt_evidence_sha256: "f".repeat(64),
        },
      }],
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Load verified receipt" }));
    await waitFor(() => expect(mocks.getHarmonizationUploadRollbackEvidence)
      .toHaveBeenCalledWith("cohort-1", "upload-1"));
    expect(screen.getByLabelText("Evidence SHA-256 *")).toHaveValue("f".repeat(64));
    await user.type(screen.getByLabelText("Rollback resolution reason *"),
      "Receipt checked against the isolated Orthanc inventory.");
    await user.click(screen.getByRole("button", { name: "Approve exact deletion" }));
    await waitFor(() => expect(mocks.resolveHarmonizationUploadRollback).toHaveBeenCalledWith(
      "cohort-1", "upload-1", "delete",
      "Receipt checked against the isolated Orthanc inventory.", "f".repeat(64)));
  });
});
