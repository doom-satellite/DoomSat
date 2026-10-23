module DoomSat {

  # ----------------------------------------------------------------------
  # Base ID Convention
  # ----------------------------------------------------------------------
  #
  # All Base IDs follow the 8-digit hex format: 0xDSSCCxxx
  #
  # Where:
  #   D   = Deployment digit (1 for this deployment)
  #   SS  = Subtopology digits (00 for main topology, 01-05 for subtopologies)
  #   CC  = Component digits (00, 01, 02, etc.)
  #   xxx = Reserved for internal component items (events, commands, telemetry)
  #

  # ----------------------------------------------------------------------
  # Defaults
  # ----------------------------------------------------------------------

  module Default {
    constant QUEUE_SIZE = 10
    constant STACK_SIZE = 64 * 1024
  }

  # ----------------------------------------------------------------------
  # Active component instances
  # ----------------------------------------------------------------------

  # 20Hz rate group (divisor 1 of the 20Hz base clock)
  instance rateGroup_20Hz: Svc.ActiveRateGroup base id 0x10001000 \
    queue size Default.QUEUE_SIZE \
    stack size Default.STACK_SIZE \
    priority 43

  # 1Hz rate group (divisor 20)
  instance rateGroup_1Hz: Svc.ActiveRateGroup base id 0x10002000 \
    queue size Default.QUEUE_SIZE \
    stack size Default.STACK_SIZE \
    priority 42

  # 0.25Hz rate group (divisor 80)
  instance rateGroup_0_25Hz: Svc.ActiveRateGroup base id 0x10003000 \
    queue size Default.QUEUE_SIZE \
    stack size Default.STACK_SIZE \
    priority 41

  instance cmdSeq: Svc.CmdSequencer base id 0x10004000 \
    queue size Default.QUEUE_SIZE \
    stack size Default.STACK_SIZE \
    priority 40

  # The Doom payload interface: commands in, telemetry and image products out
  instance doom: DoomMission.Doom base id 0x10005000 \
    queue size 60 \
    stack size Default.STACK_SIZE \
    priority 44

  # CFDP file handling: the three Svc/Subtopologies/FileHandlingCfdp instances, declared here
  # because that subtopology does not compile at F´ v4.3.0 (its configComponents phase calls
  # cfdpManager.configure(allocator) but CfdpManager::configure needs a fileQueueDepth too).
  # cfdpManager is configured in DoomSatTopology.cpp. Queue 200, not the subtopology's 30:
  # dataIn, run1Hz and pingIn are async ports that assert when the queue is full. (The dataIn backlog is
  # bounded first by commsBufferManager's 50 buffers, each queued PDU holding one.)
  instance cfdpManager: Svc.Ccsds.Cfdp.CfdpManager base id 0x10006000 \
    queue size 200 \
    stack size 128 * 1024 \
    priority 24

  instance fileManager: Svc.FileManager base id 0x10007000 \
    queue size Default.QUEUE_SIZE \
    stack size Default.STACK_SIZE \
    priority 22

  instance prmDb: Svc.PrmDb base id 0x10008000 \
    queue size Default.QUEUE_SIZE \
    stack size Default.STACK_SIZE \
    priority 21 \
  {
    phase Fpp.ToCpp.Phases.readParameters """
    DoomSat::prmDb.readParamFile();
    """
  }

  # ----------------------------------------------------------------------
  # Passive component instances
  # ----------------------------------------------------------------------

  instance chronoTime: Svc.ChronoTime base id 0x10010000

  instance rateGroupDriver: Svc.RateGroupDriver base id 0x10011000

  instance systemResources: Svc.SystemResources base id 0x10012000

  instance timer: Svc.LinuxTimer base id 0x10013000

  instance comDriver: Drv.TcpClient base id 0x10014000

  # Buffers cfdpManager allocates itself (downlinked file data and its ACK/NAK/FIN), so a downlink cannot drain
  # ComCcsds.commsBufferManager (radio receive, frame accumulator and space packet framer). Uplinked PDUs still
  # sit in commsBufferManager buffers until cfdpManager's async dataIn handles them, so an upload paced faster
  # than cfdpManager keeps up can still drain that pool and stop the downlink: keep PDU pacing at 5 ms or more
  # (findings, risk 4). Set up in DoomSatTopology.cpp.
  instance cfdpBufferManager: Svc.BufferManager base id 0x10015000

  # Between the router and cfdpManager, and between cfdpManager and the com queue: confines CFDP uploads to the
  # uplink directory and commits a WAD on the receiver's FIN (flight/Components/CfdpGuard)
  instance cfdpGuard: DoomMission.CfdpGuard base id 0x10016000

}
