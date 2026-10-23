# Scratch model, the alternative: import only the packet layer and build the frame layer locally,
# so the FARM sits between the accumulator and the deframer with no conflicting connection.
port Data(x: U32)

passive component Acc { output port dataOut: Data }
passive component Def { sync input port dataIn: Data }
passive component Farm { sync input port dataIn: Data
                         output port dataOut: Data }

module Lib {
  instance acc: Acc base id 0x100
  instance deframer: Def base id 0x200
  topology Framing {
    instance acc
    instance deframer
    connections Uplink { acc.dataOut -> deframer.dataIn }
  }
}

instance farm: Farm base id 0x300

topology Deploy {
  # Lib.Framing not imported: its instances are listed and wired here instead
  instance Lib.acc
  instance Lib.deframer
  instance farm
  connections Splice {
    Lib.acc.dataOut -> farm.dataIn
    farm.dataOut -> Lib.deframer.dataIn
  }
}
