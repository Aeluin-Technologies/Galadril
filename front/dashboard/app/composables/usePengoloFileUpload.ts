import { usePengoloUi } from "~/stores/usePengoloUi";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloChats } from "~/stores/usePengoloChats";

export function usePengoloFileUpload() {
  const ui = usePengoloUi(),
    library = usePengoloLibrary(),
    projects = usePengoloProjects();
  const { t } = useI18n();
  const chats = usePengoloChats();
  const account = useAccount();
  async function attach(files: readonly File[]) {
    const version = ui.draftVersion,
      temporary = ui.temporary;
    const chat = chats.active?.id,
      project = ui.projectId;
    const current = () =>
      version === ui.draftVersion &&
      temporary === ui.temporary &&
      chat === chats.active?.id &&
      project === ui.projectId;
    const scope =
      temporary ||
      projects.items.find((item) => item.id === ui.projectId)?.data
        .contextMode === "personal"
        ? null
        : ui.projectId;
    for (const file of files) {
      if (!current()) return;
      if (ui.files.length + ui.pendingFiles >= 8) {
        ui.error = t("pengolo.copy.a_message_supports_up_to_eight_files");
        return;
      }
      ui.pendingFiles++;
      try {
        if (temporary && !ui.temporarySession)
          ui.temporarySession = crypto.randomUUID().replaceAll("-", "");
        const ready = await library.upload(
          file,
          scope,
          temporary ? ui.temporarySession : null,
        );
        if (current() && !ui.files.some((item) => item.id === ready.id))
          ui.files.push(ready);
      } catch {
        if (current())
          ui.error = t("pengolo.copy.upload_failed_use_pdf_text_csv_png_jpeg");
        return;
      } finally {
        if (version === ui.draftVersion)
          ui.pendingFiles = Math.max(0, ui.pendingFiles - 1);
      }
    }
  }
  async function importFiles(files: readonly File[], projectId: string | null) {
    const session = account.session.value;
    const current = () =>
      session?.tenant === account.session.value?.tenant &&
      session?.subject === account.session.value?.subject;
    for (const file of files) {
      if (!current()) return;
      try {
        await library.upload(file, projectId);
      } catch {
        if (current())
          ui.error = t("pengolo.copy.upload_failed_use_pdf_text_csv_png_jpeg");
        return;
      }
    }
  }
  return { attach, importFiles };
}
