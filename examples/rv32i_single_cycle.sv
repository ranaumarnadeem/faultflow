// ============================================================================
// Flattened from HarieshAnbalagan/RV32I (single_cycle_processor), MIT licensed.
// Mechanically concatenated + type-qualified for faultflow's
// single-read_verilog-invocation synthesis flow -- see
// tests/python/test_synth_systemverilog.py and CLAUDE.md's '-sv note'
// for why package typedef'd types/enum literals are referenced as
// `risc_v_32_i_pkg::NAME` (wildcard import alone doesn't resolve them
// in this Yosys version, though it's kept -- it correctly resolves
// bare package parameters like XLEN). No logic changed.
// ============================================================================

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Package: risc_v_32_i_pkg.sv
*
* Description:
*
* Contains the parameters and enums required.
***************************************************************************/
package risc_v_32_i_pkg;

parameter XLEN = 32;
parameter REG_ADDR_WIDTH = 5;

parameter IMM_SEL_LEN = 3;

typedef enum logic[IMM_SEL_LEN-1:0] {
    IMM_B_TYPE,
    IMM_S_TYPE,
    IMM_U_TYPE,
    IMM_I_TYPE,
    IMM_J_TYPE,
    IMM_UNKNOWN_TYPE
} imm_select_e;

parameter ALU_SEL_LEN = 4;

typedef enum logic [ALU_SEL_LEN-1:0] {
    OP_ADD,
    OP_SUB,
    OP_AND,
    OP_OR,
    OP_XOR,
    OP_SLL,
    OP_SRL,
    OP_SRA,
    OP_UNKNOWN
} alu_select_e;

parameter BRANCH_SEL_LEN = 3;

typedef enum logic [BRANCH_SEL_LEN-1:0] {
    OP_BEQ,
    OP_BNE,
    OP_BLT,
    OP_BGE,
    OP_BLTU,
    OP_BGEU,
    OP_BUNKNOWN
} comp_select_e;

parameter RD_MUX_SEL_LEN = 3;

typedef enum logic [RD_MUX_SEL_LEN-1:0] {
    RD_MUX_DMEM,
    RD_MUX_ALU,
    RD_MUX_BCU,
    RD_MUX_IMM,
    RD_MUX_PC_N,
    RD_MUX_N_A
}write_data_select_e;

parameter LOAD_STORE_TYPE_LEN = 4;

typedef enum logic [LOAD_STORE_TYPE_LEN-1:0]
{
    L_W,
    L_H,
    L_HU,
    L_B,
    L_BU,
    S_W,
    S_H,
    S_B,
    LS_N_A
}load_store_type_e;

endpackage

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: RegisterFile.sv
*
* Description:
*
* This holds the architectural register file, which contains the registers
* associated with processor to store data
***************************************************************************/

module RegisterFile #(parameter XLEN = 32, parameter REG_ADDR_WIDTH = 5)
(
    output logic [XLEN-1:0]             read_data_1_o,
    output logic [XLEN-1:0]             read_data_2_o,
    input  logic                        clk_i,
    input  logic                        write_enable_i,
    input  logic [XLEN-1:0]             write_data_i,
    input  logic [REG_ADDR_WIDTH-1:0]   write_address_i,
    input  logic [REG_ADDR_WIDTH-1:0]   read_address_1_i,
    input  logic [REG_ADDR_WIDTH-1:0]   read_address_2_i
);

    logic [XLEN-1:0]register [XLEN-1:0];

    always_comb
    begin
        read_data_1_o = (read_address_1_i == 0) ? 0 : register[read_address_1_i];
        read_data_2_o = (read_address_2_i == 0) ? 0 : register[read_address_2_i];
    end

    always_ff @(negedge clk_i)
    begin
        if (write_enable_i == 1 && write_address_i != 0)
        begin
            register[write_address_i] <= write_data_i;
        end
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ImmediateSignExtend.sv
*
* Description:
*
* This sign extends the immediate value from instructions, not applicable
* for R-type instruction as it uses data from RegistreFile instead.
***************************************************************************/
import risc_v_32_i_pkg::*;

module ImmediateSignExtend #(parameter XLEN = 32)
(
    output logic        [XLEN-1:0]  imm_o,
    input  logic        [XLEN-1:7]  imm_i,
    input  risc_v_32_i_pkg::imm_select_e             imm_sel_i
);

    always_comb
    begin
        unique case(imm_sel_i)
            risc_v_32_i_pkg::IMM_B_TYPE: imm_o = $signed({imm_i[31], imm_i[7], imm_i[30:25], imm_i[11:8], 1'b0});
            risc_v_32_i_pkg::IMM_S_TYPE: imm_o = $signed({imm_i[31:25], imm_i[11:7]});
            risc_v_32_i_pkg::IMM_U_TYPE: imm_o = {imm_i[31:12], 12'b0};
            risc_v_32_i_pkg::IMM_I_TYPE: imm_o = $signed({imm_i[31:20]});
            risc_v_32_i_pkg::IMM_J_TYPE: imm_o = $signed({imm_i[31], imm_i[19:12], imm_i[20], imm_i[30:21], 1'b0});
            risc_v_32_i_pkg::IMM_UNKNOWN_TYPE: imm_o = 0;
        endcase
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ArithmeticLogicUnit.sv
*
* Description:
*
* This does computation over the data related to arithmetic and logic
* instructions. This does not holds the comparison which is done by
* ComparatorUnit.
***************************************************************************/
import risc_v_32_i_pkg::*;

module ArithmeticLogicUnit #(parameter XLEN = 32, parameter REG_ADDR_WIDTH = 5)
(
    output logic        [XLEN-1:0]  alu_o,
    input  logic        [XLEN-1:0]  alu_port_a_i,
    input  logic        [XLEN-1:0]  alu_port_b_i,
    input  risc_v_32_i_pkg::alu_select_e             alu_op_sel_i
);

    always_comb
    begin
        unique case(alu_op_sel_i)
            risc_v_32_i_pkg::OP_ADD:     alu_o =         alu_port_a_i  +   alu_port_b_i;
            risc_v_32_i_pkg::OP_SUB:     alu_o =         alu_port_a_i  -   alu_port_b_i;
            risc_v_32_i_pkg::OP_AND:     alu_o =         alu_port_a_i  &   alu_port_b_i;
             risc_v_32_i_pkg::OP_OR:     alu_o =         alu_port_a_i  |   alu_port_b_i;
            risc_v_32_i_pkg::OP_XOR:     alu_o =         alu_port_a_i  ^   alu_port_b_i;
            risc_v_32_i_pkg::OP_SLL:     alu_o =         alu_port_a_i  <<  alu_port_b_i[REG_ADDR_WIDTH-1:0];
            risc_v_32_i_pkg::OP_SRL:     alu_o =         alu_port_a_i  >>  alu_port_b_i[REG_ADDR_WIDTH-1:0];
            risc_v_32_i_pkg::OP_SRA:     alu_o = $signed(alu_port_a_i) >>> alu_port_b_i[REG_ADDR_WIDTH-1:0];
        risc_v_32_i_pkg::OP_UNKNOWN:     alu_o = {XLEN{1'b0}};
        endcase
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ComparatorUnit.sv
*
* Description:
*
* This does computation over the data related to comparison operations.
* The final output is a singe bit. This is kept seperatly from ALU such that
* ALU does addition and this unit does comparision at same time to avoid 
* seperate adder for branch operation.
***************************************************************************/
import risc_v_32_i_pkg::*;

module ComparatorUnit #(parameter XLEN = 32)
(
    output logic                        comp_o,
    input  logic           [XLEN-1:0]   comp_port_a_i,
    input  logic           [XLEN-1:0]   comp_port_b_i,
    input  risc_v_32_i_pkg::comp_select_e                comp_op_sel_i
);

    always_comb
    begin
        unique case(comp_op_sel_i)
            risc_v_32_i_pkg::OP_BEQ:     comp_o =         comp_port_a_i  ==  comp_port_b_i;
            risc_v_32_i_pkg::OP_BNE:     comp_o =         comp_port_a_i  !=  comp_port_b_i;
           risc_v_32_i_pkg::OP_BLTU:     comp_o =         comp_port_a_i  <   comp_port_b_i;
           risc_v_32_i_pkg::OP_BGEU:     comp_o =         comp_port_a_i  >=  comp_port_b_i;
            risc_v_32_i_pkg::OP_BLT:     comp_o = $signed(comp_port_a_i) <   $signed(comp_port_b_i);
            risc_v_32_i_pkg::OP_BGE:     comp_o = $signed(comp_port_a_i) >=  $signed(comp_port_b_i);
       risc_v_32_i_pkg::OP_BUNKNOWN:     comp_o = 1'b0;
        endcase
    end
endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: LoadAndStoreUnit.sv
*
* Description:
*
* This module handles the load and store opertaion between processor
* and memeory elements. This is not complete yet to be developed fully.
***************************************************************************/
import risc_v_32_i_pkg::*;

module LoadAndStoreUnit #(parameter XLEN = 32)
(
    output logic [XLEN-1:0]     read_data_o,
    input  logic [XLEN-1:0]     read_data_i,
    output logic [XLEN-1:0]     write_data_o,
    input  logic [XLEN-1:0]     write_data_i,
    input risc_v_32_i_pkg::load_store_type_e     load_store_type_i,
    output logic [3:0]          write_data_strobe_o
);

    always_comb
    begin
        unique case(load_store_type_i)
               risc_v_32_i_pkg::L_B: read_data_o =               $signed(read_data_i[(XLEN/4)-1:0]);
              risc_v_32_i_pkg::L_BU: read_data_o = {{((3*XLEN)/4){1'b0}},read_data_i[(XLEN/4)-1:0]};
               risc_v_32_i_pkg::L_H: read_data_o =               $signed(read_data_i[(XLEN/2)-1:0]);
              risc_v_32_i_pkg::L_HU: read_data_o = {{(( XLEN /2)){1'b0}},read_data_i[(XLEN/2)-1:0]};
               risc_v_32_i_pkg::L_W: read_data_o =                       read_data_i[XLEN-1:0];
           default: read_data_o =                       read_data_i[XLEN-1:0];
        endcase
    end

    always_comb
    begin
        unique case(load_store_type_i)
               risc_v_32_i_pkg::S_B:
                   begin
                   write_data_o = {{((3*XLEN)/4){1'b0}},write_data_i[(XLEN/4)-1:0]};
                   write_data_strobe_o = 4'b0001;
                   end
               risc_v_32_i_pkg::S_H:
                   begin
                   write_data_o = {{(( XLEN /2)){1'b0}},write_data_i[(XLEN/2)-1:0]};
                   write_data_strobe_o = 4'b0011;
                   end
               risc_v_32_i_pkg::S_W:
                   begin
                   write_data_o = write_data_i[XLEN-1:0];
                   write_data_strobe_o = 4'b1111;
                   end
           default:
                   begin
                   write_data_o = write_data_i[XLEN-1:0];
                   write_data_strobe_o = 4'b0000;
                   end
        endcase
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: CoreControlUnit.sv
*
* Description:
*
* This does control of the processor core datapath
***************************************************************************/
import risc_v_32_i_pkg::*;

module CoreControlUnit #(parameter XLEN = 32)
(
    output logic                    pc_mux_sel_o,
    output logic                    reg_write_enable_o,
    output risc_v_32_i_pkg::imm_select_e             imm_select_o,
    output logic                    execute_port_a_sel_o,
    output logic                    execute_port_b_sel_o,
    output risc_v_32_i_pkg::alu_select_e             alu_op_sel_o,
    output risc_v_32_i_pkg::comp_select_e            comp_op_sel_o,
    output risc_v_32_i_pkg::load_store_type_e        load_store_type_o,
    output logic                    data_memory_write_enable_o,
    output risc_v_32_i_pkg::write_data_select_e      reg_write_data_sel_o,
    input logic [6:0]               op_code_i,
    input logic [2:0]               funct3_i,
    input logic                     funct7_bit5_i,
    input logic                     branch_enable_i
);

    localparam OP_R_TYPE        = 7'b0110011;
    localparam OP_B_TYPE        = 7'b1100011;
    localparam OP_S_TYPE        = 7'b0100011;
    localparam OP_I_JALR_TYPE   = 7'b1100111;
    localparam OP_I_LOAD_TYPE   = 7'b0000011;
    localparam OP_I_ALU_TYPE    = 7'b0010011;
    localparam OP_I_FENCE_TYPE  = 7'b0001111;
    localparam OP_I_ECALL_TYPE  = 7'b1110011;
    localparam OP_U_LUI_TYPE    = 7'b0110111;
    localparam OP_U_AUIPC_TYPE  = 7'b0010111;
    localparam OP_J_TYPE        = 7'b1101111;

    always_comb
    begin

        unique case(op_code_i)
            OP_R_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_UNKNOWN_TYPE;
                execute_port_a_sel_o = 1'b1;

                unique case({funct7_bit5_i,funct3_i})
                    4'b0000:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b1000:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SUB;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0001:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SLL;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0010:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BLT;
                            execute_port_b_sel_o = 1'b0;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                            end
                    4'b0011:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BLTU;
                            execute_port_b_sel_o = 1'b0;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                            end
                    4'b0100:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_XOR;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0101:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SRL;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b1101:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SRA;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0110:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_OR;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0111:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_AND;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    default:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                endcase

                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
            end
            OP_B_TYPE:
            begin
                pc_mux_sel_o = branch_enable_i;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_B_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;

                unique case(funct3_i)
                    3'b000: comp_op_sel_o = risc_v_32_i_pkg::OP_BEQ;
                    3'b001: comp_op_sel_o = risc_v_32_i_pkg::OP_BNE;
                    3'b100: comp_op_sel_o = risc_v_32_i_pkg::OP_BLT;
                    3'b101: comp_op_sel_o = risc_v_32_i_pkg::OP_BGE;
                    3'b110: comp_op_sel_o = risc_v_32_i_pkg::OP_BLTU;
                    3'b111: comp_op_sel_o = risc_v_32_i_pkg::OP_BGEU;
                   default: comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                endcase

                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_S_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_S_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;

                unique case(funct3_i)
                    3'b000:  load_store_type_o = risc_v_32_i_pkg::S_B;
                    3'b001:  load_store_type_o = risc_v_32_i_pkg::S_H;
                    3'b010:  load_store_type_o = risc_v_32_i_pkg::S_W;
                endcase

                data_memory_write_enable_o = 1'b1;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_I_JALR_TYPE:
            begin
                pc_mux_sel_o = 1'b1;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_PC_N;
            end
            OP_I_LOAD_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;

                unique case(funct3_i)
                    3'b000:  load_store_type_o = risc_v_32_i_pkg::L_B;
                    3'b001:  load_store_type_o = risc_v_32_i_pkg::L_H;
                    3'b010:  load_store_type_o = risc_v_32_i_pkg::L_W;
                    3'b100:  load_store_type_o = risc_v_32_i_pkg::L_BU;
                    3'b101:  load_store_type_o = risc_v_32_i_pkg::L_HU;
                    default: load_store_type_o = risc_v_32_i_pkg::L_W;
                endcase

                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_DMEM;
            end
            OP_I_ALU_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;

                unique case(funct3_i)
                    3'b000:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b010:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BLT;
                           execute_port_b_sel_o = 1'b1;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                           end
                    3'b011:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BLTU;
                           execute_port_b_sel_o = 1'b1;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                           end
                    3'b100:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_XOR;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b110:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_OR;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b111:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_AND;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b001:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_SLL;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b101:
                           begin
                           alu_op_sel_o = ((funct7_bit5_i)? risc_v_32_i_pkg::OP_SRA : risc_v_32_i_pkg::OP_SRL);
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                   default:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                endcase

                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
            end
            OP_I_FENCE_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_I_ECALL_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_U_LUI_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_U_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_IMM;
            end
            OP_U_AUIPC_TYPE:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_U_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
            end
            OP_J_TYPE:
            begin
                pc_mux_sel_o = 1'b1;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_J_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_PC_N;
            end
            default:
            begin
                pc_mux_sel_o = 1'b0;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_UNKNOWN_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
        endcase

    end
endmodule

/***************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ProcessorCore.sv
*
* Description:
*
* This does contains to processor core datapath.
* (Load and store are yet to developed fully)
***************************************************************/
import risc_v_32_i_pkg::*;

module ProcessorCore
(
    input logic             clk_i,
    input logic             reset_i,

    output logic [31:0]     instruction_address_o,
     input logic [31:0]     instruction_data_i,

     input logic [31:0]     read_data_i,
    output logic            write_enable_o,
    output logic [31:0]     write_data_o,
    output logic [3:0]      write_data_strobe_o,
    output logic [31:0]     address_o
);
    logic [31:0]PC;
    logic [31:0]pc_next,pc_update;

    logic [31:0] instruction;

    logic [31:0]immediate;
    logic [31:0]reg_source_1;
    logic [31:0]reg_source_2;

    logic [31:0]alu_port_a;
    logic [31:0]alu_port_b;
    logic [31:0]comp_port_a;
    logic [31:0]comp_port_b;

    logic [31:0]alu_output;
    logic branch_enable;

    logic [31:0]read_data_aligned;
    logic [31:0]write_data_unaligned;
    risc_v_32_i_pkg::load_store_type_e load_store_type;

    logic [31:0]dmem_data;

    logic pc_mux_sel;
    logic reg_write_enable;
    risc_v_32_i_pkg::imm_select_e imm_select;
    logic execute_port_a_sel;
    logic execute_port_b_sel;
    risc_v_32_i_pkg::alu_select_e alu_op_sel;
    risc_v_32_i_pkg::comp_select_e comp_op_sel;
    logic data_memory_write_enable;
    risc_v_32_i_pkg::write_data_select_e reg_write_data_sel;

    logic [31:0]reg_data;

    assign pc_next = PC + 32'd4;
    assign pc_update = pc_mux_sel ? alu_output : pc_next;

    always_ff @(posedge clk_i, posedge reset_i)
    begin
        if (reset_i)
        begin
            PC <= 32'd0;
        end
        else
        begin
            PC <= pc_update;
        end
    end

    assign instruction_address_o = PC;
    assign instruction = instruction_data_i;

    RegisterFile #(.XLEN(XLEN), .REG_ADDR_WIDTH(REG_ADDR_WIDTH)) regfile
    (
        .read_data_1_o      (reg_source_1),
        .read_data_2_o      (reg_source_2),
        .clk_i              (clk_i),
        .write_enable_i     (reg_write_enable),
        .write_data_i       (reg_data),
        .write_address_i    (instruction[11:7]),
        .read_address_1_i   (instruction[19:15]),
        .read_address_2_i   (instruction[24:20])
    );

    ImmediateSignExtend #(.XLEN(XLEN)) ise
    (
        .imm_o      (immediate),
        .imm_i      (instruction[31:7]),
        .imm_sel_i  (imm_select)
    );

    CoreControlUnit ccu
    (
        .pc_mux_sel_o                     (pc_mux_sel),
        .reg_write_enable_o               (reg_write_enable),
        .imm_select_o                     (imm_select),
        .execute_port_a_sel_o             (execute_port_a_sel),
        .execute_port_b_sel_o             (execute_port_b_sel),
        .alu_op_sel_o                     (alu_op_sel),
        .comp_op_sel_o                    (comp_op_sel),
        .load_store_type_o                (load_store_type),
        .data_memory_write_enable_o       (data_memory_write_enable),
        .reg_write_data_sel_o             (reg_write_data_sel),
        .op_code_i                        (instruction[6:0]),
        .funct3_i                         (instruction[14:12]),
        .funct7_bit5_i                    (instruction[30]),
        .branch_enable_i                  (branch_enable)
    );

    assign alu_port_a = execute_port_a_sel ? reg_source_1 : PC;
    assign alu_port_b = execute_port_b_sel ? reg_source_2 : immediate;
    assign comp_port_a = reg_source_1;
    assign comp_port_b = execute_port_b_sel ? immediate : reg_source_2;

    ArithmeticLogicUnit #(.XLEN(XLEN), .REG_ADDR_WIDTH(REG_ADDR_WIDTH)) alu
    (
        .alu_o          (alu_output),
        .alu_port_a_i   (alu_port_a),
        .alu_port_b_i   (alu_port_b),
        .alu_op_sel_i   (alu_op_sel)
    );

    ComparatorUnit #(.XLEN(XLEN)) bcu
    (
        .comp_o         (branch_enable),
        .comp_port_a_i    (comp_port_a),
        .comp_port_b_i    (comp_port_b),
        .comp_op_sel_i    (comp_op_sel)
    );

    LoadAndStoreUnit #(.XLEN(XLEN)) lsu
    (
        .read_data_o         (read_data_aligned),
        .read_data_i         (read_data_i),
        .write_data_o        (write_data_o),
        .write_data_i        (write_data_unaligned),
        .load_store_type_i   (load_store_type),
        .write_data_strobe_o (write_data_strobe_o)
    );

    assign address_o = alu_output;
    assign dmem_data = read_data_aligned;
    assign write_enable_o = data_memory_write_enable;
    assign write_data_unaligned = reg_source_2;

    always_comb
    begin
        unique case(reg_write_data_sel)
            risc_v_32_i_pkg::RD_MUX_DMEM:    reg_data = dmem_data;
             risc_v_32_i_pkg::RD_MUX_ALU:    reg_data = alu_output;
             risc_v_32_i_pkg::RD_MUX_BCU:    reg_data = {31'b0, branch_enable};
             risc_v_32_i_pkg::RD_MUX_IMM:    reg_data = immediate;
            risc_v_32_i_pkg::RD_MUX_PC_N:    reg_data = pc_next;
                default:    reg_data = {XLEN{1'bz}};
        endcase
    end

endmodule
