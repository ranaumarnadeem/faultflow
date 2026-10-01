// Simulation model of the fixture's memory macro: port 0 read/write with a
// write mask, port 1 read-only. Port 0 behaves like openMBIST's OpenRAM model
// (rtl/input_demo_8x16_scn4m.v), the timing autoMBIST's read_latency 0 is set
// for: inputs registered on the rising edge, the write and the read on the
// falling edge, read data DELAY after it.
//
// Faults, for the BIST tests:
//   STUCK_ADDR >= 0   bit STUCK_BIT of that word always reads STUCK_VALUE;
//   FAIL_ON_READ > 0  the port-0 read with that number (1 = the first) returns
//                     its data with bit 0 flipped -- a fault only one compare
//                     sees, e.g. the last.
// `reads` counts port-0 reads.
`timescale 1ns/1ps
module input_demo_8x16_scn4m #(
    parameter integer ADDR_WIDTH = 4,
    parameter integer DATA_WIDTH = 8,
    parameter integer NUM_SPARE_ROWS = 0,
    parameter integer NUM_SPARE_COLS = 0,
    parameter integer DELAY = 3,
    parameter integer T_HOLD = 1,
    parameter integer STUCK_ADDR = -1,
    parameter integer STUCK_BIT = 0,
    parameter integer STUCK_VALUE = 0,
    parameter integer FAIL_ON_READ = 0
) (
    input  wire       clk0,
    input  wire       csb0,
    input  wire       web0,
    input  wire       wmask0,
    input  wire [3:0] addr0,
    input  wire [7:0] din0,
    output reg  [7:0] dout0,
    input  wire       clk1,
    input  wire       csb1,
    input  wire [3:0] addr1,
    output reg  [7:0] dout1
);
    reg [7:0] mem [0:15];
    reg       csb0_reg, web0_reg, wmask0_reg;
    reg [3:0] addr0_reg, addr1_reg;
    reg [7:0] din0_reg;
    reg       csb1_reg;
    integer   reads = 0;

    function [7:0] stored(input [3:0] addr);
        begin
            stored = mem[addr];
            if (STUCK_ADDR >= 0 && addr == STUCK_ADDR)
                stored[STUCK_BIT] = STUCK_VALUE[0];
        end
    endfunction

    always @(posedge clk0) begin
        csb0_reg = csb0;
        web0_reg = web0;
        wmask0_reg = wmask0;
        addr0_reg = addr0;
        din0_reg = din0;
        #(T_HOLD) dout0 = 8'bx;
    end

    always @(negedge clk0) begin
        if (!csb0_reg && !web0_reg && wmask0_reg)
            mem[addr0_reg] = din0_reg;
        if (!csb0_reg && web0_reg) begin
            reads = reads + 1;
            dout0 <= #(DELAY) stored(addr0_reg) ^ ((reads == FAIL_ON_READ) ? 8'h01 : 8'h00);
        end
    end

    always @(posedge clk1) begin
        csb1_reg = csb1;
        addr1_reg = addr1;
        #(T_HOLD) dout1 = 8'bx;
    end

    always @(negedge clk1)
        if (!csb1_reg)
            dout1 <= #(DELAY) stored(addr1_reg);
endmodule
