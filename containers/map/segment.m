% SPM12 unified segmentation for MAP-style voxel morphometry (Huppertz MAP07 substrate, §25.4).
%
% Runs headless in SPM12 Standalone (MCR) via `spm12 script segment.m`. The worker mounts a
% per-subject work dir at /work and places the (gunzipped) T1 at /work/T1.nii. SPM writes, into
% /work:
%   c1T1.nii c2T1.nii c3T1.nii   native-space tissue segments (GM/WM/CSF)
%   wc1T1.nii wc2T1.nii          warped (MNI) unmodulated tissue probabilities  <- morphometry input
%   mwc1T1.nii mwc2T1.nii        warped (MNI) modulated (Jacobian-scaled) GM/WM  <- VBM volume
%   y_T1.nii iy_T1.nii           forward / inverse deformation fields (native<->MNI)
%
% The junction/extension feature maps + single-subject z-scoring are computed downstream in the
% pkg container (map_morphometry.py, which has nibabel/scipy) — image math is far cleaner there
% than in MATLAB. This script only produces the standard SPM tissue segmentation + normalisation.

spm('defaults','fmri');
spm_jobman('initcfg');

tpm = '/opt/spm12/spm12_mcr/spm12/spm12/tpm/TPM.nii';
if ~exist(tpm, 'file')
    tpm = fullfile(spm('Dir'), 'tpm', 'TPM.nii');   % fallback if the image layout changes
end

clear matlabbatch;
matlabbatch{1}.spm.spatial.preproc.channel.vols     = {'/work/T1.nii,1'};
matlabbatch{1}.spm.spatial.preproc.channel.biasreg  = 0.001;
matlabbatch{1}.spm.spatial.preproc.channel.biasfwhm = 60;
matlabbatch{1}.spm.spatial.preproc.channel.write    = [0 1];   % write bias-corrected T1

% SPM12 default tissue settings; write native + warped for GM(1)/WM(2), native for CSF(3).
ngaus  = [1 1 2 3 4 2];
native = [1 1 1 0 0 0];     % write native c1..c3
warped = [1 1 0 0 0 0];     % write warped GM/WM (wc + mwc)
for t = 1:6
    matlabbatch{1}.spm.spatial.preproc.tissue(t).tpm    = {sprintf('%s,%d', tpm, t)};
    matlabbatch{1}.spm.spatial.preproc.tissue(t).ngaus  = ngaus(t);
    matlabbatch{1}.spm.spatial.preproc.tissue(t).native = [native(t) 0];
    matlabbatch{1}.spm.spatial.preproc.tissue(t).warped = [warped(t) warped(t)];  % [mod unmod]
end

matlabbatch{1}.spm.spatial.preproc.warp.mrf     = 1;
matlabbatch{1}.spm.spatial.preproc.warp.cleanup = 1;
matlabbatch{1}.spm.spatial.preproc.warp.reg     = [0 0.001 0.5 0.05 0.2];
matlabbatch{1}.spm.spatial.preproc.warp.affreg  = 'mni';
matlabbatch{1}.spm.spatial.preproc.warp.fwhm    = 0;
matlabbatch{1}.spm.spatial.preproc.warp.samp    = 3;
matlabbatch{1}.spm.spatial.preproc.warp.write   = [1 1];   % [inverse forward] deformations
matlabbatch{1}.spm.spatial.preproc.warp.vox     = NaN;
matlabbatch{1}.spm.spatial.preproc.warp.bb      = [NaN NaN NaN; NaN NaN NaN];

spm_jobman('run', matlabbatch);
