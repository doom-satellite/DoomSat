module DoomSat {

  # ----------------------------------------------------------------------
  # Symbolic constants for port numbers
  # ----------------------------------------------------------------------

  enum Ports_RateGroups {
    rateGroup_20Hz
    rateGroup_1Hz
    rateGroup_0_25Hz
  }

  deployment topology DoomSat {

  # ----------------------------------------------------------------------
  # Subtopology imports
  # ----------------------------------------------------------------------
    import CdhCore.Subtopology
    import ComCcsds.Subtopology
    import DataProducts.Subtopology

  # ----------------------------------------------------------------------
  # Instances used in the topology
  # ----------------------------------------------------------------------
    instance chronoTime
    instance rateGroup_20Hz
    instance rateGroup_1Hz
    instance rateGroup_0_25Hz
    instance rateGroupDriver
    instance systemResources
    instance timer
    instance comDriver
    instance cmdSeq
    instance doom
    instance cfdpManager
    instance cfdpBufferManager
    instance fileManager
    instance prmDb

  # ----------------------------------------------------------------------
  # Pattern graph specifiers
  # ----------------------------------------------------------------------

    command connections instance CdhCore.cmdDisp
    event connections instance CdhCore.events
    telemetry connections instance CdhCore.tlmSend
    text event connections instance CdhCore.textLogger
    health connections instance CdhCore.$health
    param connections instance prmDb
    time connections instance chronoTime

  # ----------------------------------------------------------------------
  # Direct graph specifiers
  # ----------------------------------------------------------------------

    connections ComCcsds_CdhCore {
      # Core events and telemetry to communication queue
      CdhCore.events.PktSend -> ComCcsds.comQueue.comPacketQueueIn[ComCcsds.Ports_ComPacketQueue.EVENTS]
      CdhCore.tlmSend.PktSend -> ComCcsds.comQueue.comPacketQueueIn[ComCcsds.Ports_ComPacketQueue.TELEMETRY]

      # Router to Command Dispatcher
      ComCcsds.fprimeRouter.commandOut -> CdhCore.cmdDisp.seqCmdBuff
      CdhCore.cmdDisp.seqCmdStatus -> ComCcsds.fprimeRouter.cmdResponseIn
    }

    connections ComCcsds_Cfdp {
      # CFDP downlink: PDUs (FW_PACKET_FILE descriptor + PDU) into the FILE buffer queue, so APID 3.
      # Both channels share the one FILE slot; every command-reachable channel must be wired,
      # because an unconnected output port asserts when the engine uses it.
      cfdpManager.dataOut[0] -> ComCcsds.comQueue.bufferQueueIn[ComCcsds.Ports_ComBufferQueue.FILE]
      cfdpManager.dataOut[1] -> ComCcsds.comQueue.bufferQueueIn[ComCcsds.Ports_ComBufferQueue.FILE]
      # dataReturnIn only deallocates, and both channels allocate from cfdpBufferManager,
      # so returning every FILE buffer on port 0 is safe
      ComCcsds.comQueue.bufferReturnOut[ComCcsds.Ports_ComBufferQueue.FILE] -> cfdpManager.dataReturnIn[0]

      # CFDP uplink: the router's FW_PACKET_FILE (APID 3) output feeds channel 0
      ComCcsds.fprimeRouter.fileOut -> cfdpManager.dataIn[0]
      cfdpManager.dataInReturn[0] -> ComCcsds.fprimeRouter.fileBufferReturnIn
      cfdpManager.dataInReturn[1] -> ComCcsds.fprimeRouter.fileBufferReturnIn

      # PDU buffers cfdpManager allocates (downlinked file data, ACK/NAK/FIN) come from a dedicated pool, not
      # ComCcsds.commsBufferManager. Uplinked PDUs still sit in commsBufferManager buffers until dataIn takes them.
      cfdpManager.bufferAllocate[0]   -> cfdpBufferManager.bufferGetCallee
      cfdpManager.bufferAllocate[1]   -> cfdpBufferManager.bufferGetCallee
      cfdpManager.bufferDeallocate[0] -> cfdpBufferManager.bufferSendIn
      cfdpManager.bufferDeallocate[1] -> cfdpBufferManager.bufferSendIn
    }

    connections Communications {
      # ComDriver buffer allocations
      comDriver.allocate      -> ComCcsds.commsBufferManager.bufferGetCallee
      comDriver.deallocate    -> ComCcsds.commsBufferManager.bufferSendIn

      # ComDriver <-> ComStub (Uplink)
      comDriver.$recv                     -> ComCcsds.comStub.drvReceiveIn
      ComCcsds.comStub.drvReceiveReturnOut -> comDriver.recvReturnIn

      # ComStub <-> ComDriver (Downlink)
      ComCcsds.comStub.drvSendOut      -> comDriver.$send
      comDriver.ready         -> ComCcsds.comStub.drvConnected
    }

    # FileHandling_Doom is gone: CfdpManager has no fileAnnounce-style output, so doom.fileAnnounce is left
    # unconnected (it is a sync input, so that is legal). The ground's COMMIT_WAD renames the .part once the
    # Class 2 transfer has its FIN, and only if the file's size and checksum are the ones the ground sent.

    connections Cfdp_DataProducts {
      # Data Products downlink over CFDP (channel, class, keep, priority and destination entity
      # come from the FileInDefault* parameters)
      DataProducts.dpCat.fileOut -> cfdpManager.fileIn
      cfdpManager.fileDoneOut -> DataProducts.dpCat.fileDone
    }

    connections RateGroups {
      # timer to drive rate group
      timer.CycleOut -> rateGroupDriver.CycleIn

      # 20Hz rate group: the payload poll, telemetry sampling and the TM frame flush
      rateGroupDriver.CycleOut[Ports_RateGroups.rateGroup_20Hz] -> rateGroup_20Hz.CycleIn
      rateGroup_20Hz.RateGroupMemberOut[0] -> doom.run
      rateGroup_20Hz.RateGroupMemberOut[1] -> CdhCore.tlmSend.Run
      rateGroup_20Hz.RateGroupMemberOut[2] -> ComCcsds.aggregator.timeout

      # 1Hz rate group
      rateGroupDriver.CycleOut[Ports_RateGroups.rateGroup_1Hz] -> rateGroup_1Hz.CycleIn
      rateGroup_1Hz.RateGroupMemberOut[0] -> cfdpManager.run1Hz
      rateGroup_1Hz.RateGroupMemberOut[1] -> systemResources.run
      rateGroup_1Hz.RateGroupMemberOut[2] -> ComCcsds.comQueue.run
      rateGroup_1Hz.RateGroupMemberOut[3] -> CdhCore.cmdDisp.run
      rateGroup_1Hz.RateGroupMemberOut[4] -> cmdSeq.schedIn

      # 0.25Hz rate group
      rateGroupDriver.CycleOut[Ports_RateGroups.rateGroup_0_25Hz] -> rateGroup_0_25Hz.CycleIn
      rateGroup_0_25Hz.RateGroupMemberOut[0] -> CdhCore.$health.Run
      rateGroup_0_25Hz.RateGroupMemberOut[1] -> ComCcsds.commsBufferManager.schedIn
      rateGroup_0_25Hz.RateGroupMemberOut[2] -> DataProducts.dpBufferManager.schedIn
      rateGroup_0_25Hz.RateGroupMemberOut[3] -> DataProducts.dpWriter.schedIn
      rateGroup_0_25Hz.RateGroupMemberOut[4] -> DataProducts.dpMgr.schedIn
      rateGroup_0_25Hz.RateGroupMemberOut[5] -> cfdpBufferManager.schedIn
    }

    connections CdhCore_cmdSeq {
      # Command Sequencer
      cmdSeq.comCmdOut -> CdhCore.cmdDisp.seqCmdBuff
      CdhCore.cmdDisp.seqCmdStatus -> cmdSeq.cmdResponseIn
    }

    connections DoomSat {
      # Image products bypass TlmChan sampling: every chunk is its own telemetry packet in the
      # same queue (and APID) as sampled telemetry.
      doom.frameOut -> ComCcsds.comQueue.comPacketQueueIn[ComCcsds.Ports_ComPacketQueue.TELEMETRY]
    }

  }

}
