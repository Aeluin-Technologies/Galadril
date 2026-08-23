interface RequestStagingResponse {
  requestStagingUpload: {
    uploadUrl: string;
    stagingKey: string;
  };
}

interface CompleteUploadResponse {
  completeUpload: string;
}

export function useS3Upload() {
  const { t } = useI18n();
  const isUploading = ref(false);
  const error = ref<string | null>(null);

  const REQUEST_STAGING_MUTATION = gql`
    mutation RequestStaging {
      requestStagingUpload {
        uploadUrl
        stagingKey
      }
    }
  `;

  const COMPLETE_UPLOAD_MUTATION = gql`
    mutation CompleteUpload($stagingKey: String!, $targetName: String!) {
      completeUpload(stagingKey: $stagingKey, targetName: $targetName)
    }
  `;

  const { mutate: requestStagingMutate } = useMutation<RequestStagingResponse>(
    REQUEST_STAGING_MUTATION,
  );
  const { mutate: completeUploadMutate } = useMutation<CompleteUploadResponse>(
    COMPLETE_UPLOAD_MUTATION,
  );

  /**
   * Initiates a staging upload slot by requesting presigned credentials.
   * @returns {Promise<{ uploadUrl: string; stagingKey: string } | null>}
   */
  async function requestStagingUpload(): Promise<{
    uploadUrl: string;
    stagingKey: string;
  } | null> {
    error.value = null;
    try {
      const response = await requestStagingMutate();
      if (
        response &&
        "data" in response &&
        response.data?.requestStagingUpload
      ) {
        return response.data.requestStagingUpload;
      }
      throw new Error(t("storage.upload.default_error"));
    } catch (exception: unknown) {
      error.value =
        exception instanceof Error
          ? exception.message
          : t("storage.upload.default_error");
      return null;
    }
  }

  /**
   * Uploads a raw file object directly to S3 using a presigned PUT URL.
   * @param {string} url - The pre-authorized S3 upload target URL.
   * @param {File} file - The binary payload file descriptor.
   * @returns {Promise<boolean>}
   */
  async function uploadToS3Presigned(
    url: string,
    file: File,
  ): Promise<boolean> {
    try {
      const response = await fetch(url, {
        method: "PUT",
        body: file,
        headers: {
          "Content-Type": file.type,
        },
      });
      return response.ok;
    } catch (e) {
      error.value = t("storage.upload.s3_error");
      return false;
    }
  }

  /**
   * Finalizes the owner-only upload lifecycle.
   * @param {string} stagingKey - The temporary bucket key allocated for staging.
   * @param {string} targetName - The destination filename asset descriptor.
   * @returns {Promise<string | null>}
   */
  async function completeUpload(
    stagingKey: string,
    targetName: string,
  ): Promise<string | null> {
    try {
      const response = await completeUploadMutate({
        stagingKey,
        targetName,
      });

      if (response && "data" in response && response.data?.completeUpload) {
        return response.data.completeUpload;
      }
      throw new Error(t("storage.upload.complete_error"));
    } catch (exception: unknown) {
      error.value =
        exception instanceof Error
          ? exception.message
          : t("storage.upload.complete_error");
      return null;
    }
  }

  return {
    isUploading,
    error,
    requestStagingUpload,
    uploadToS3Presigned,
    completeUpload,
  };
}
