/* Private integration-test build settings; installed binaries do not use these.
 * Runtime socket paths let all workers share the same compiled executables. */
#define SCU_COMPUTER_SOCKET getenv("SCU_TEST_COMPUTER_SOCKET")
#define SCU_STATUS_TIMEOUT_MS (getenv("SCU_TEST_STATUS_TIMEOUT_MS") \
    ? atoi(getenv("SCU_TEST_STATUS_TIMEOUT_MS")) : 30000)
