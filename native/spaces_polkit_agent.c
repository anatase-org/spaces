#include <errno.h>
#define POLKIT_AGENT_I_KNOW_API_IS_SUBJECT_TO_CHANGE
#include <polkitagent/polkitagent.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/prctl.h>
#include <unistd.h>

typedef struct {
    PolkitAgentListener parent;
} SpacesListener;

typedef struct {
    PolkitAgentListenerClass parent;
} SpacesListenerClass;

typedef struct {
    GTask *task;
    PolkitAgentSession *session;
} Authentication;

G_DEFINE_TYPE(SpacesListener, spaces_listener, POLKIT_AGENT_TYPE_LISTENER)

static void authentication_free(Authentication *authentication)
{
    g_clear_object(&authentication->session);
    g_clear_object(&authentication->task);
    g_free(authentication);
}

static void authentication_completed(
    PolkitAgentSession *session,
    gboolean gained_authorization,
    gpointer user_data)
{
    Authentication *authentication = user_data;

    (void)session;
    g_task_return_boolean(authentication->task, gained_authorization);
    authentication_free(authentication);
}

static void unexpected_prompt(
    PolkitAgentSession *session,
    const gchar *request,
    gboolean echo_on,
    gpointer user_data)
{
    (void)request;
    (void)echo_on;
    (void)user_data;
    /*
     * pam_spaces performs the complete host conversation. A prompt reaching
     * this guest-only agent means that a different PAM module was selected;
     * never collect a credential in that case.
     */
    polkit_agent_session_cancel(session);
}

static PolkitIdentity *identity_for_uid(GList *identities, uid_t uid)
{
    for (GList *item = identities; item != NULL; item = item->next) {
        PolkitIdentity *identity = POLKIT_IDENTITY(item->data);

        if (POLKIT_IS_UNIX_USER(identity) &&
            (uid_t)polkit_unix_user_get_uid(
                POLKIT_UNIX_USER(identity)) == uid)
            return identity;
    }
    return NULL;
}

static void initiate_authentication(
    PolkitAgentListener *listener,
    const gchar *action_id,
    const gchar *message,
    const gchar *icon_name,
    PolkitDetails *details,
    const gchar *cookie,
    GList *identities,
    GCancellable *cancellable,
    GAsyncReadyCallback callback,
    gpointer user_data)
{
    PolkitIdentity *identity;
    Authentication *authentication;

    (void)action_id;
    (void)message;
    (void)icon_name;
    (void)details;

    identity = identity_for_uid(identities, getuid());
    if (identity == NULL) {
        g_task_report_new_error(
            listener,
            callback,
            user_data,
            initiate_authentication,
            POLKIT_ERROR,
            POLKIT_ERROR_NOT_AUTHORIZED,
            "The current guest user is not an authentication identity");
        return;
    }

    authentication = g_new0(Authentication, 1);
    authentication->task = g_task_new(listener, cancellable, callback, user_data);
    authentication->session = polkit_agent_session_new(identity, cookie);
    g_signal_connect(
        authentication->session,
        "completed",
        G_CALLBACK(authentication_completed),
        authentication);
    g_signal_connect(
        authentication->session,
        "request",
        G_CALLBACK(unexpected_prompt),
        authentication);
    polkit_agent_session_initiate(authentication->session);
}

static gboolean initiate_authentication_finish(
    PolkitAgentListener *listener,
    GAsyncResult *result,
    GError **error)
{
    (void)listener;
    return g_task_propagate_boolean(G_TASK(result), error);
}

static void spaces_listener_class_init(SpacesListenerClass *class)
{
    PolkitAgentListenerClass *listener_class =
        POLKIT_AGENT_LISTENER_CLASS(class);

    listener_class->initiate_authentication = initiate_authentication;
    listener_class->initiate_authentication_finish =
        initiate_authentication_finish;
}

static void spaces_listener_init(SpacesListener *listener)
{
    (void)listener;
}

static gboolean harden(void)
{
    struct rlimit limit = {0, 0};

    if (setrlimit(RLIMIT_CORE, &limit) != 0)
        return FALSE;
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        return FALSE;
    return TRUE;
}

static gboolean safe_session(const char *value)
{
    size_t size = strlen(value);
    if (size == 0 || size > 128)
        return FALSE;
    for (; *value != '\0'; value++) {
        if (
            !((*value >= 'a' && *value <= 'z') ||
              (*value >= 'A' && *value <= 'Z') ||
              (*value >= '0' && *value <= '9') ||
              *value == '_' || *value == '-')
        )
            return FALSE;
    }
    return TRUE;
}

int main(int argc, char **argv)
{
    PolkitAgentListener *listener;
    PolkitSubject *subject;
    GMainLoop *loop;
    GError *error = NULL;
    gpointer registration;
    const char *session_id;

    if (
        argc != 3 ||
        strcmp(argv[1], "--session") != 0 ||
        !safe_session(argv[2])
    ) {
        return EXIT_FAILURE;
    }
    session_id = argv[2];
    if (!harden())
        return EXIT_FAILURE;

    listener = g_object_new(spaces_listener_get_type(), NULL);
    subject = polkit_unix_session_new(session_id);

    registration = polkit_agent_listener_register(
        listener,
        POLKIT_AGENT_REGISTER_FLAGS_NONE,
        subject,
        "/org/anatase/Spaces/AuthenticationAgent",
        NULL,
        &error);
    if (registration == NULL) {
        g_printerr("Could not register the guest Polkit agent: %s\n",
                   error->message);
        g_error_free(error);
        g_object_unref(subject);
        g_object_unref(listener);
        return EXIT_FAILURE;
    }

    loop = g_main_loop_new(NULL, FALSE);
    g_main_loop_run(loop);
    g_main_loop_unref(loop);
    polkit_agent_listener_unregister(registration);
    g_object_unref(subject);
    g_object_unref(listener);
    return EXIT_SUCCESS;
}
