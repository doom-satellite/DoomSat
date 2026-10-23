// ======================================================================
// \title  CfdpCfg.hpp
// \author Brian Campuzano
// \brief  F Prime CFDP configuration constants
// ======================================================================

#include <config/FppConstantsAc.hpp>

namespace Svc {
namespace Ccsds {
namespace Cfdp {

// ==================================================================
// Protocol Configuration
// ==================================================================

/**
 *  @brief RX chunks per transaction (per channel)
 *
 *  @par Description:
 *       Number of chunks per transaction per channel (RX).
 *
 * RX CHUNKS -
 * For Class 2 CFDP receive transactions, the receiver must track which file segments
 * have been successfully received. A chunk represents a contiguous range (offset, size)
 * of received file data. By tracking received chunks, the receiver can identify gaps
 * in the file data and generate NAK PDUs to request retransmission of missing segments.
 *
 * (array size must be NumChannels)
 * CFDP_CHANNEL_NUM_RX_CHUNKS_PER_TRANSACTION is an array for each channel indicating
 * the number of chunks per transaction to track received file segments. This enables
 * gap detection and NAK generation for reliable Class 2 transfers.
 *
 *  @par Limits:
 *
 */
// DoomSat: 2048 on channel 0 (stock: NakMaxSegments, 58). With 58, a receive that loses more than about 57 runs of
// PDUs forgets data it already has, and a later NAK (once the gaps it can still see are refilled) asks for the rest
// of the file again: a 4.2 MB upload at 5 % loss was sent about 5,590 more PDUs to cover 238 lost. 2048 covers a
// 28.8 MB IWAD at 5 % loss. Each NAK still carries at most NakMaxSegments gaps, so k gaps take about k / 58 rounds,
// each after an ack_timer with no data; and cfdpManager's sentNakSegmentRequests then counts the gaps found per NAK,
// not the segments sent (TransactionRx.cpp rSubstateSendNak). 2048 x 8 bytes x 50 transactions: about 800 KB.
#define CFDP_CHANNEL_NUM_RX_CHUNKS_PER_TRANSACTION {2048, NakMaxSegments}

/**
 *  @brief TX chunks per transaction (per channel)
 *
 *  @par Description:
 *       Number of chunks per transaction per channel (TX).
 *
 * TX CHUNKS -
 * For Class 2 CFDP transmit transactions, the sender must track which file segments
 * the receiver has requested via NAK PDUs. Each chunk represents a gap (offset, size)
 * that needs to be retransmitted.
 *
 * (array size must be NumChannels)
 * CFDP_CHANNEL_NUM_TX_CHUNKS_PER_TRANSACTION is an array for each channel indicating
 * the number of chunks to track NAK segment requests from the receiver per transaction.
 * This allows the sender to queue and retransmit the requested missing file data.
 *
 *  @par Limits:
 *
 */
#define CFDP_CHANNEL_NUM_TX_CHUNKS_PER_TRANSACTION {NakMaxSegments, NakMaxSegments}

// ==================================================================
// Miscellaneous
// ==================================================================

/**
 * @brief Macro type for Entity id that is used in printf style formatting
 *
 * @note This must match the size of CfdpEntityId as defined in CfdpCfg.fpp
 */
#define CFDP_PRI_ENTITY_ID PRIu32

/**
 * @brief Macro type for transaction sequences that is used in printf style formatting
 *
 * @note This must match the size of CfdpTransactionSeq as defined in CfdpCfg.fpp
 */
#define CFDP_PRI_TRANSACTION_SEQ PRIu32

}  // namespace Cfdp
}  // namespace Ccsds
}  // namespace Svc
